// wa-media: download WhatsApp media whose CDN links have expired.
//
// Usage: wa-media [--type image] [--chat PHONE_OR_JID,...] [--limit N] [--dry-run]
//
// Undownloaded media is read from <session>/messages.db (table "messages").
// Files are downloaded directly when the CDN link still works. Otherwise the
// phone is asked to re-upload them (media retry receipt), and the file is
// fetched from the new path.
package main

import (
	"context"
	"database/sql"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"

	"github.com/joshuaswanson/messages-cli/go/whatsapp/internal/wasession"
)

const (
	connectTimeout = 20 * time.Second
	batchSize      = 10
)

// MediaEntry is an undownloaded media item from messages.db.
type MediaEntry struct {
	ID            string
	ChatJID       string
	Sender        string
	MediaType     string
	DirectPath    string
	MimeType      string
	MediaKey      []byte
	FileSHA256    []byte
	FileEncSHA256 []byte
	FileLength    int64
	Timestamp     time.Time
}

type retryResult struct {
	DirectPath string
	Err        error
}

type pendingRetry struct {
	entry  MediaEntry
	doneCh chan retryResult
}

var (
	pendingRetries   = make(map[string]*pendingRetry)
	pendingRetriesMu sync.Mutex
)

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}

func main() {
	sessionDir := flag.String("session", filepath.Join(os.Getenv("HOME"), ".whatsapp-cli"), "whatsmeow session directory")
	outputDir := flag.String("output", filepath.Join(os.Getenv("HOME"), ".whatsapp-cli", "media"), "media output directory")
	mediaType := flag.String("type", "image", "media type to download (image, video, audio, document)")
	limit := flag.Int("limit", 0, "max items to process (0 = all)")
	retryTimeout := flag.Duration("retry-timeout", 30*time.Second, "timeout waiting for media retry responses")
	dryRun := flag.Bool("dry-run", false, "list what would be downloaded without connecting")
	chatFilter := flag.String("chat", "", "only these chats (comma-separated phone numbers or JIDs)")
	flag.Parse()

	if _, err := toWhatsmeowMediaType(*mediaType); err != nil {
		fail("%v", err)
	}

	messagesDBPath := filepath.Join(*sessionDir, "messages.db")
	if _, err := os.Stat(messagesDBPath); os.IsNotExist(err) {
		fail("Media index not found at %s", messagesDBPath)
	}
	entries, err := loadUndownloadedMedia(messagesDBPath, *mediaType, *limit)
	if err != nil {
		fail("Failed to load media entries: %v", err)
	}

	if *chatFilter != "" {
		allowed, err := resolveChatFilter(messagesDBPath, *chatFilter)
		if err != nil {
			fail("Failed to resolve chat filter: %v", err)
		}
		if len(allowed) == 0 {
			fail("No chats matched filter: %s", *chatFilter)
		}
		filtered := entries[:0]
		for _, e := range entries {
			if allowed[e.ChatJID] {
				filtered = append(filtered, e)
			}
		}
		entries = filtered
	}

	fmt.Printf("Found %d undownloaded %s item(s)\n", len(entries), *mediaType)
	if *dryRun {
		for i, e := range entries {
			fmt.Printf("  [%d] %s in %s (%d bytes)\n", i+1, e.ID, e.ChatJID, e.FileLength)
		}
		return
	}
	if len(entries) == 0 {
		return
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigChan
		fmt.Println("\nInterrupted, shutting down...")
		cancel()
	}()

	_, device, err := wasession.Open(ctx, *sessionDir)
	if err != nil {
		fail("%v", err)
	}
	if device.ID == nil {
		fail("Session is not paired. Run 'messages auth whatsapp' first.")
	}
	wasession.UseLatestVersion(ctx)
	client := whatsmeow.NewClient(device, waLog.Noop)
	client.AddEventHandler(func(evt any) {
		if v, ok := evt.(*events.MediaRetry); ok {
			handleMediaRetry(v)
		}
	})
	err = wasession.Connect(ctx, client, connectTimeout)
	if errors.Is(err, wasession.ErrLoggedOut) {
		fail("WhatsApp session was logged out on the phone. Run 'messages auth whatsapp' to pair again.")
	}
	if err != nil {
		fail("%v", err)
	}
	defer client.Disconnect()

	var stats downloadStats
	for start := 0; start < len(entries); start += batchSize {
		if ctx.Err() != nil {
			stats.skipped += len(entries) - start
			break
		}
		end := min(start+batchSize, len(entries))
		processBatch(ctx, client, entries[start:end], start, len(entries), *outputDir, *retryTimeout, messagesDBPath, &stats)
	}
	fmt.Printf("\nDone. Downloaded: %d, Retried: %d, Failed: %d, Skipped: %d\n",
		stats.downloaded, stats.retried, stats.failed, stats.skipped)
}

func loadUndownloadedMedia(dbPath, mediaType string, limit int) ([]MediaEntry, error) {
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()

	query := `SELECT id, chat_jid, sender, media_type, direct_path, mime_type,
	                 media_key, file_sha256, file_enc_sha256, file_length, timestamp
	          FROM messages
	          WHERE media_type = ?
	            AND (local_path IS NULL OR local_path = '')
	            AND direct_path IS NOT NULL
	            AND media_key IS NOT NULL
	          ORDER BY timestamp DESC`
	args := []any{mediaType}
	if limit > 0 {
		query += " LIMIT ?"
		args = append(args, limit)
	}

	rows, err := db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var entries []MediaEntry
	for rows.Next() {
		var e MediaEntry
		var sender, mime sql.NullString
		var length sql.NullInt64
		var ts sql.NullTime
		if err := rows.Scan(&e.ID, &e.ChatJID, &sender, &e.MediaType, &e.DirectPath, &mime,
			&e.MediaKey, &e.FileSHA256, &e.FileEncSHA256, &length, &ts); err != nil {
			return nil, err
		}
		e.Sender, e.MimeType, e.FileLength, e.Timestamp = sender.String, mime.String, length.Int64, ts.Time
		entries = append(entries, e)
	}
	return entries, rows.Err()
}

type downloadStats struct {
	downloaded, retried, failed, skipped int
}

type batchItem struct {
	idx        int
	entry      MediaEntry
	targetPath string
	doneCh     chan retryResult
}

func shortID(id string) string {
	if len(id) > 12 {
		return id[:12]
	}
	return id
}

func processBatch(ctx context.Context, client *whatsmeow.Client, batch []MediaEntry, offset, total int,
	outputDir string, retryTimeout time.Duration, messagesDBPath string, stats *downloadStats) {
	// Phase 1: direct downloads, collecting items whose CDN link expired.
	var needRetry []batchItem
	for i, entry := range batch {
		if ctx.Err() != nil {
			stats.skipped += len(batch) - i
			return
		}
		idx := offset + i + 1
		fmt.Printf("[%d/%d] %s... ", idx, total, shortID(entry.ID))

		chatDir := filepath.Join(outputDir, sanitizePath(entry.ChatJID))
		if err := os.MkdirAll(chatDir, 0o700); err != nil {
			fmt.Println("FAILED (mkdir)")
			stats.failed++
			continue
		}
		targetPath := filepath.Join(chatDir, sanitizePath(entry.ID)+extensionForMime(entry.MimeType))
		if _, err := os.Stat(targetPath); err == nil {
			fmt.Println("SKIP (exists)")
			stats.skipped++
			continue
		}

		data, err := download(ctx, client, entry, entry.DirectPath)
		switch {
		case err == nil:
			if saveFile(targetPath, data, messagesDBPath, entry) {
				fmt.Printf("OK (%d bytes)\n", len(data))
				stats.downloaded++
			} else {
				fmt.Println("FAILED (write)")
				stats.failed++
			}
		case isExpiredError(err):
			fmt.Println("expired")
			needRetry = append(needRetry, batchItem{idx: idx, entry: entry, targetPath: targetPath})
		default:
			fmt.Printf("FAILED (%v)\n", err)
			stats.failed++
		}
	}
	if len(needRetry) == 0 {
		return
	}

	// Phase 2: ask the phone to re-upload every expired item.
	fmt.Printf("  Sending %d retry requests...\n", len(needRetry))
	for i := range needRetry {
		item := &needRetry[i]
		item.doneCh = make(chan retryResult, 1)

		chatJID, err := types.ParseJID(item.entry.ChatJID)
		if err != nil {
			item.doneCh <- retryResult{Err: err}
			continue
		}
		senderJID := chatJID
		if parsed, err := types.ParseJID(item.entry.Sender); err == nil && item.entry.Sender != "" {
			senderJID = parsed
		}
		info := &types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:    chatJID,
				Sender:  senderJID,
				IsGroup: chatJID.Server == types.GroupServer,
			},
			ID:        types.MessageID(item.entry.ID),
			Timestamp: item.entry.Timestamp,
		}

		pendingRetriesMu.Lock()
		pendingRetries[item.entry.ID] = &pendingRetry{entry: item.entry, doneCh: item.doneCh}
		pendingRetriesMu.Unlock()

		if err := client.SendMediaRetryReceipt(ctx, info, item.entry.MediaKey); err != nil {
			forgetRetry(item.entry.ID)
			item.doneCh <- retryResult{Err: err}
		}
		time.Sleep(100 * time.Millisecond) // spacing between receipts to avoid rate limits
	}

	// Phase 3: wait for the re-upload notifications with one shared deadline.
	fmt.Printf("  Waiting for responses (timeout %v)...\n", retryTimeout)
	deadline, cancel := context.WithTimeout(ctx, retryTimeout)
	defer cancel()
	for _, item := range needRetry {
		select {
		case result := <-item.doneCh:
			forgetRetry(item.entry.ID)
			if result.Err != nil {
				fmt.Printf("  [%d/%d] %s RETRY FAILED (%v)\n", item.idx, total, shortID(item.entry.ID), result.Err)
				stats.failed++
				continue
			}
			data, err := download(ctx, client, item.entry, result.DirectPath)
			if err != nil {
				fmt.Printf("  [%d/%d] %s DOWNLOAD FAILED (%v)\n", item.idx, total, shortID(item.entry.ID), err)
				stats.failed++
				continue
			}
			if !saveFile(item.targetPath, data, messagesDBPath, item.entry) {
				fmt.Printf("  [%d/%d] %s WRITE FAILED\n", item.idx, total, shortID(item.entry.ID))
				stats.failed++
				continue
			}
			fmt.Printf("  [%d/%d] %s OK (%d bytes)\n", item.idx, total, shortID(item.entry.ID), len(data))
			stats.downloaded++
			stats.retried++
		case <-deadline.Done():
			forgetRetry(item.entry.ID)
			if ctx.Err() != nil {
				stats.skipped++
			} else {
				fmt.Printf("  [%d/%d] %s TIMED OUT\n", item.idx, total, shortID(item.entry.ID))
				stats.failed++
			}
		}
	}
}

func forgetRetry(id string) {
	pendingRetriesMu.Lock()
	delete(pendingRetries, id)
	pendingRetriesMu.Unlock()
}

// download fetches and decrypts a media file. After a media retry, pass the
// new direct path; the original media key stays valid.
func download(ctx context.Context, client *whatsmeow.Client, entry MediaEntry, directPath string) ([]byte, error) {
	mediaType, err := toWhatsmeowMediaType(entry.MediaType)
	if err != nil {
		return nil, err
	}
	return client.DownloadMediaWithPath(ctx, directPath, entry.FileEncSHA256, entry.FileSHA256,
		entry.MediaKey, mediaType, "", false)
}

func saveFile(path string, data []byte, messagesDBPath string, entry MediaEntry) bool {
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return false
	}
	updateLocalPath(messagesDBPath, entry.ID, entry.ChatJID, path)
	return true
}

func handleMediaRetry(evt *events.MediaRetry) {
	pendingRetriesMu.Lock()
	pending, ok := pendingRetries[string(evt.MessageID)]
	pendingRetriesMu.Unlock()
	if !ok {
		return
	}
	if evt.Error != nil {
		pending.doneCh <- retryResult{Err: fmt.Errorf("media retry error code %d", evt.Error.Code)}
		return
	}
	data, err := whatsmeow.DecryptMediaRetryNotification(evt, pending.entry.MediaKey)
	if err != nil {
		pending.doneCh <- retryResult{Err: fmt.Errorf("decrypt retry: %w", err)}
		return
	}
	if data.GetResult() != waMmsRetry.MediaRetryNotification_SUCCESS {
		pending.doneCh <- retryResult{Err: fmt.Errorf("retry result: %s", data.GetResult())}
		return
	}
	pending.doneCh <- retryResult{DirectPath: data.GetDirectPath()}
}

func isExpiredError(err error) bool {
	return errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403)
}

func toWhatsmeowMediaType(mediaType string) (whatsmeow.MediaType, error) {
	switch strings.ToLower(mediaType) {
	case "image":
		return whatsmeow.MediaImage, nil
	case "video":
		return whatsmeow.MediaVideo, nil
	case "audio":
		return whatsmeow.MediaAudio, nil
	case "document":
		return whatsmeow.MediaDocument, nil
	default:
		return "", fmt.Errorf("unsupported media type: %s (use image, video, audio, or document)", mediaType)
	}
}

func extensionForMime(mime string) string {
	switch {
	case strings.Contains(mime, "jpeg") || strings.Contains(mime, "jpg"):
		return ".jpg"
	case strings.Contains(mime, "png"):
		return ".png"
	case strings.Contains(mime, "webp"):
		return ".webp"
	case strings.Contains(mime, "gif"):
		return ".gif"
	case strings.Contains(mime, "mp4"):
		return ".mp4"
	case strings.Contains(mime, "ogg"):
		return ".ogg"
	case strings.Contains(mime, "pdf"):
		return ".pdf"
	default:
		return ".bin"
	}
}

func sanitizePath(s string) string {
	return strings.Map(func(r rune) rune {
		if strings.ContainsRune(`/\:*?"<>|`, r) {
			return '_'
		}
		return r
	}, s)
}

func updateLocalPath(dbPath, msgID, chatJID, localPath string) {
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		return
	}
	defer db.Close()
	_, _ = db.Exec("UPDATE messages SET local_path = ?, downloaded_at = ? WHERE id = ? AND chat_jid = ?",
		localPath, time.Now(), msgID, chatJID)
}

// resolveChatFilter maps comma-separated phone numbers or JIDs to chat JIDs in messages.db.
func resolveChatFilter(dbPath, filter string) (map[string]bool, error) {
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()

	rows, err := db.Query("SELECT DISTINCT chat_jid FROM messages WHERE chat_jid IS NOT NULL")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var allJIDs []string
	for rows.Next() {
		var jid string
		if err := rows.Scan(&jid); err != nil {
			return nil, err
		}
		allJIDs = append(allJIDs, jid)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	result := make(map[string]bool)
	for _, term := range strings.Split(filter, ",") {
		term = strings.TrimSpace(term)
		if term == "" {
			continue
		}
		if strings.Contains(term, "@") {
			result[term] = true
			continue
		}
		digits := strings.Map(func(r rune) rune {
			if r >= '0' && r <= '9' {
				return r
			}
			return -1
		}, term)
		digits = strings.TrimLeft(digits, "0")
		if len(digits) < 7 {
			continue
		}
		for _, jid := range allJIDs {
			if strings.Contains(strings.Split(jid, "@")[0], digits) {
				result[jid] = true
			}
		}
	}
	return result, nil
}
