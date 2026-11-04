// wa-media: download WhatsApp media that WhatsApp Desktop has not saved locally.
//
// Usage: wa-media [--type image,video] [--chat NAME_PHONE_OR_JID,...] [--limit N] [--dry-run]
//
// whatsmeow is an unofficial client, and WhatsApp may restrict accounts that
// show automated, high-volume behavior. Re-upload requests are therefore
// spaced out (--retry-interval) and runs are capped at 20 items by default.
//
// Media is listed from WhatsApp Desktop's ChatStorage.sqlite (opened
// read-only). Each item is downloaded from the WhatsApp CDN when its link
// still works. Otherwise the phone is asked to re-upload it (media retry
// receipt) and the file is fetched from the new path. Files are saved as
// <output>/<chat JID>/<message ID>.<ext>, where messages-cli finds them.
package main

import (
	"context"
	"database/sql"
	"errors"
	"flag"
	"fmt"
	"net/url"
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
	// Seconds between 1970-01-01 and 2001-01-01 (CoreData reference date).
	coreDataEpoch = 978307200
)

var chatStorage = filepath.Join(os.Getenv("HOME"),
	"Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite")

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}

func main() {
	sessionDir := flag.String("session", filepath.Join(os.Getenv("HOME"), ".whatsapp-cli"), "whatsmeow session directory")
	outputDir := flag.String("output", filepath.Join(os.Getenv("HOME"), ".whatsapp-cli", "media"), "media output directory")
	typeFlag := flag.String("type", "image", "comma-separated media types: image, video, audio, document, sticker, or all")
	limit := flag.Int("limit", 20, "max items to process, newest first (0 = all; large runs send many re-upload requests from an unofficial client)")
	retryTimeout := flag.Duration("retry-timeout", 30*time.Second, "timeout waiting for media retry responses")
	retryInterval := flag.Duration("retry-interval", time.Second, "pause between re-upload requests to the phone")
	dryRun := flag.Bool("dry-run", false, "list what would be downloaded without connecting")
	chatFilter := flag.String("chat", "", "only these chats (comma-separated names, phone numbers, or JIDs)")
	includeStatus := flag.Bool("include-status", false, "include status updates (phones usually delete them after 24 hours)")
	flag.Parse()

	kinds, err := parseKinds(*typeFlag)
	if err != nil {
		fail("%v", err)
	}
	db, err := openReadOnly(chatStorage)
	if err != nil {
		fail("Cannot open the WhatsApp Desktop database (grant Full Disk Access to your terminal): %v", err)
	}
	defer db.Close()

	entries, err := loadMedia(db, kinds, *chatFilter, *includeStatus)
	if err != nil {
		fail("Failed to list media: %v", err)
	}
	var todo []MediaEntry
	for _, e := range entries {
		if _, err := os.Stat(targetPath(*outputDir, e)); err != nil {
			todo = append(todo, e)
		}
	}
	fmt.Printf("%d undownloaded item(s), %d already in %s\n", len(todo), len(entries)-len(todo), *outputDir)
	if *limit > 0 && len(todo) > *limit {
		todo = todo[:*limit]
		fmt.Printf("Processing the newest %d (use --limit to change)\n", len(todo))
	}

	if *dryRun {
		for i, e := range todo {
			fmt.Printf("  [%d] %s %s in %s (%d bytes, %s)\n", i+1, e.Kind.Name, e.ID, e.ChatName, e.FileLength, e.Timestamp.Format("2006-01-02"))
		}
		return
	}
	if len(todo) == 0 {
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
	for start := 0; start < len(todo); start += batchSize {
		if ctx.Err() != nil {
			stats.skipped += len(todo) - start
			break
		}
		end := min(start+batchSize, len(todo))
		processBatch(ctx, client, todo[start:end], start, len(todo), *outputDir, *retryTimeout, *retryInterval, &stats)
	}
	fmt.Printf("\nDone. Downloaded: %d, Retried: %d, Failed: %d, Skipped: %d\n",
		stats.downloaded, stats.retried, stats.failed, stats.skipped)
}

// ---------------------------------------------------------------------------
// Media listing
// ---------------------------------------------------------------------------

// MediaKind maps a WhatsApp Desktop ZMESSAGETYPE to a download type.
type MediaKind struct {
	Name        string
	MessageType int
	MediaType   whatsmeow.MediaType
	Extension   string
}

var mediaKinds = []MediaKind{
	{"image", 1, whatsmeow.MediaImage, ".jpg"},
	{"video", 2, whatsmeow.MediaVideo, ".mp4"},
	{"audio", 3, whatsmeow.MediaAudio, ".ogg"},
	{"document", 8, whatsmeow.MediaDocument, ".bin"},
	{"sticker", 15, whatsmeow.MediaImage, ".webp"},
}

func parseKinds(value string) ([]MediaKind, error) {
	if strings.TrimSpace(value) == "all" {
		return mediaKinds, nil
	}
	var kinds []MediaKind
	for _, name := range strings.Split(value, ",") {
		name = strings.ToLower(strings.TrimSpace(name))
		found := false
		for _, k := range mediaKinds {
			if k.Name == name {
				kinds = append(kinds, k)
				found = true
			}
		}
		if !found {
			return nil, fmt.Errorf("unsupported media type %q (use image, video, audio, document, sticker, or all)", name)
		}
	}
	return kinds, nil
}

// MediaEntry is one undownloaded media item.
type MediaEntry struct {
	ID            string
	ChatJID       string
	ChatName      string
	SenderJID     string
	IsFromMe      bool
	Kind          MediaKind
	DirectPath    string
	MediaKey      []byte
	FileEncSHA256 []byte
	FileLength    int64
	Filename      string
	Timestamp     time.Time
}

func openReadOnly(path string) (*sql.DB, error) {
	dsn := (&url.URL{Scheme: "file", Path: path, RawQuery: "mode=ro"}).String()
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, err
	}
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func isStatusChat(jid string) bool {
	return jid == "status@broadcast" || strings.HasSuffix(jid, "@status") || strings.HasSuffix(jid, ".status")
}

func loadMedia(db *sql.DB, kinds []MediaKind, chatFilter string, includeStatus bool) ([]MediaEntry, error) {
	byType := map[int]MediaKind{}
	placeholders := make([]string, len(kinds))
	args := make([]any, len(kinds))
	for i, k := range kinds {
		byType[k.MessageType] = k
		placeholders[i] = "?"
		args[i] = k.MessageType
	}
	rows, err := db.Query(`
		SELECT m.ZSTANZAID, c.ZCONTACTJID, COALESCE(c.ZPARTNERNAME, ''), COALESCE(gm.ZMEMBERJID, ''),
		       m.ZISFROMME, m.ZMESSAGETYPE, CAST(m.ZMESSAGEDATE + `+fmt.Sprint(coreDataEpoch)+` AS INTEGER),
		       mi.ZMEDIAURL, mi.ZMEDIAKEY, COALESCE(mi.ZFILESIZE, 0), COALESCE(mi.ZTITLE, '')
		FROM ZWAMEDIAITEM mi
		JOIN ZWAMESSAGE m ON mi.ZMESSAGE = m.Z_PK
		JOIN ZWACHATSESSION c ON m.ZCHATSESSION = c.Z_PK
		LEFT JOIN ZWAGROUPMEMBER gm ON m.ZGROUPMEMBER = gm.Z_PK
		WHERE m.ZMESSAGETYPE IN (`+strings.Join(placeholders, ",")+`)
		  AND mi.ZMEDIALOCALPATH IS NULL
		  AND mi.ZMEDIAKEY IS NOT NULL
		  AND mi.ZMEDIAURL IS NOT NULL
		  AND m.ZSTANZAID IS NOT NULL
		ORDER BY m.ZMESSAGEDATE DESC`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	filter := newChatFilter(chatFilter)
	var entries []MediaEntry
	for rows.Next() {
		var e MediaEntry
		var messageType int
		var unixTime int64
		var mediaURL string
		var keyBlob []byte
		if err := rows.Scan(&e.ID, &e.ChatJID, &e.ChatName, &e.SenderJID, &e.IsFromMe, &messageType, &unixTime,
			&mediaURL, &keyBlob, &e.FileLength, &e.Filename); err != nil {
			return nil, err
		}
		if !includeStatus && isStatusChat(e.ChatJID) {
			continue
		}
		if !filter.matches(e.ChatJID, e.ChatName) {
			continue
		}
		directPath, ok := directPathFromURL(mediaURL)
		if !ok {
			continue // link previews and other non-WhatsApp URLs
		}
		mediaKey, encHash, ok := parseMediaKey(keyBlob)
		if !ok {
			continue
		}
		e.Kind = byType[messageType]
		e.DirectPath, e.MediaKey, e.FileEncSHA256 = directPath, mediaKey, encHash
		e.Timestamp = time.Unix(unixTime, 0)
		if e.ChatName == "" {
			e.ChatName = e.ChatJID
		}
		entries = append(entries, e)
	}
	return entries, rows.Err()
}

// directPathFromURL turns a WhatsApp CDN URL into the direct path whatsmeow
// downloads from (path plus query).
func directPathFromURL(raw string) (string, bool) {
	u, err := url.Parse(raw)
	if err != nil || !strings.HasSuffix(u.Hostname(), ".whatsapp.net") || !strings.HasPrefix(u.Path, "/") {
		return "", false
	}
	if u.RawQuery == "" {
		return u.EscapedPath(), true
	}
	return u.EscapedPath() + "?" + u.RawQuery, true
}

// ---------------------------------------------------------------------------
// Chat filter
// ---------------------------------------------------------------------------

type chatFilter struct {
	terms []string
}

func newChatFilter(value string) chatFilter {
	var terms []string
	for _, t := range strings.Split(value, ",") {
		if t = strings.TrimSpace(t); t != "" {
			terms = append(terms, t)
		}
	}
	return chatFilter{terms}
}

func digitsOf(s string) string {
	return strings.TrimLeft(strings.Map(func(r rune) rune {
		if r >= '0' && r <= '9' {
			return r
		}
		return -1
	}, s), "0")
}

func looksLikePhone(s string) bool {
	for _, r := range s {
		if !strings.ContainsRune("+0123456789 -().", r) {
			return false
		}
	}
	return len(digitsOf(s)) >= 7
}

func (f chatFilter) matches(jid, name string) bool {
	if len(f.terms) == 0 {
		return true
	}
	user := strings.Split(jid, "@")[0]
	for _, t := range f.terms {
		switch {
		case strings.Contains(t, "@"):
			if t == jid {
				return true
			}
		case looksLikePhone(t):
			if strings.Contains(user, digitsOf(t)) {
				return true
			}
		default:
			if strings.Contains(strings.ToLower(name), strings.ToLower(t)) {
				return true
			}
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Output paths
// ---------------------------------------------------------------------------

func sanitizePath(s string) string {
	return strings.Map(func(r rune) rune {
		if strings.ContainsRune(`/\:*?"<>|`, r) {
			return '_'
		}
		return r
	}, s)
}

// targetPath must stay in sync with messages_cli/whatsapp_db.py, which looks
// files up as <output>/<sanitized chat JID>/<sanitized message ID>.*.
func targetPath(outputDir string, e MediaEntry) string {
	ext := e.Kind.Extension
	if e.Kind.Name == "document" {
		if docExt := filepath.Ext(e.Filename); docExt != "" && len(docExt) <= 6 {
			ext = strings.ToLower(docExt)
		}
	}
	return filepath.Join(outputDir, sanitizePath(e.ChatJID), sanitizePath(e.ID)+ext)
}

// ---------------------------------------------------------------------------
// Downloading
// ---------------------------------------------------------------------------

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

type downloadStats struct {
	downloaded, retried, failed, skipped int
}

type batchItem struct {
	idx    int
	entry  MediaEntry
	path   string
	doneCh chan retryResult
}

func shortID(id string) string {
	if len(id) > 12 {
		return id[:12]
	}
	return id
}

func processBatch(ctx context.Context, client *whatsmeow.Client, batch []MediaEntry, offset, total int,
	outputDir string, retryTimeout, retryInterval time.Duration, stats *downloadStats) {
	// Phase 1: direct downloads, collecting items whose CDN link expired.
	var needRetry []batchItem
	for i, entry := range batch {
		if ctx.Err() != nil {
			stats.skipped += len(batch) - i
			return
		}
		idx := offset + i + 1
		path := targetPath(outputDir, entry)
		fmt.Printf("[%d/%d] %s %s in %s... ", idx, total, entry.Kind.Name, shortID(entry.ID), entry.ChatName)

		data, err := download(ctx, client, entry, entry.DirectPath)
		switch {
		case err == nil:
			if saveFile(path, data) {
				fmt.Printf("OK (%d bytes)\n", len(data))
				stats.downloaded++
			} else {
				fmt.Println("FAILED (write)")
				stats.failed++
			}
		case isExpiredError(err):
			fmt.Println("expired, will ask the phone")
			needRetry = append(needRetry, batchItem{idx: idx, entry: entry, path: path})
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
		info, err := messageInfo(client, item.entry)
		if err != nil {
			item.doneCh <- retryResult{Err: err}
			continue
		}

		pendingRetriesMu.Lock()
		pendingRetries[item.entry.ID] = &pendingRetry{entry: item.entry, doneCh: item.doneCh}
		pendingRetriesMu.Unlock()

		if err := client.SendMediaRetryReceipt(ctx, info, item.entry.MediaKey); err != nil {
			forgetRetry(item.entry.ID)
			item.doneCh <- retryResult{Err: err}
		}
		select {
		case <-ctx.Done():
		case <-time.After(retryInterval):
		}
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
			if !saveFile(item.path, data) {
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
				fmt.Printf("  [%d/%d] %s TIMED OUT (is the phone online?)\n", item.idx, total, shortID(item.entry.ID))
				stats.failed++
			}
		}
	}
}

func messageInfo(client *whatsmeow.Client, e MediaEntry) (*types.MessageInfo, error) {
	chat, err := types.ParseJID(e.ChatJID)
	if err != nil {
		return nil, err
	}
	sender := chat
	switch {
	case e.IsFromMe && client.Store.ID != nil:
		sender = client.Store.ID.ToNonAD()
	case e.SenderJID != "":
		if parsed, err := types.ParseJID(e.SenderJID); err == nil {
			sender = parsed
		}
	}
	return &types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     chat,
			Sender:   sender,
			IsFromMe: e.IsFromMe,
			IsGroup:  chat.Server == types.GroupServer,
		},
		ID:        types.MessageID(e.ID),
		Timestamp: e.Timestamp,
	}, nil
}

func forgetRetry(id string) {
	pendingRetriesMu.Lock()
	delete(pendingRetries, id)
	pendingRetriesMu.Unlock()
}

// download fetches and decrypts a media file. After a media retry, pass the
// new direct path; the original media key stays valid.
//
// WhatsApp Desktop does not store the plaintext hash. whatsmeow still compares
// the plaintext against it when allowNoHash is set and returns
// ErrInvalidMediaSHA256 together with the decrypted data. That comparison runs
// after the encrypted-file hash and the MAC have been verified, so the data is
// authentic; the expected size is checked as well.
func download(ctx context.Context, client *whatsmeow.Client, e MediaEntry, directPath string) ([]byte, error) {
	data, err := client.DownloadMediaWithPath(ctx, directPath, e.FileEncSHA256, nil, e.MediaKey, e.Kind.MediaType, "", true)
	if errors.Is(err, whatsmeow.ErrInvalidMediaSHA256) && len(data) > 0 {
		if e.FileLength > 0 && int64(len(data)) != e.FileLength {
			return nil, fmt.Errorf("decrypted size %d does not match expected %d", len(data), e.FileLength)
		}
		return data, nil
	}
	return data, err
}

func saveFile(path string, data []byte) bool {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return false
	}
	tmp := path + ".part"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return false
	}
	return os.Rename(tmp, path) == nil
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
