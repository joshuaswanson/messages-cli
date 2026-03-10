// fb-threads-tool: Fetch all Messenger threads via MQTT pagination.
//
// Usage: fb-threads-tool <cookies_json_path> [max_pages]
//
// Outputs JSON array of threads to stdout. Logs go to stderr.
// Each thread has: thread_id, name, last_activity_ms, snippet, thread_type.
// Default max_pages is 100 (~1500 threads). Set to 0 for unlimited.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/rs/zerolog"

	"go.mau.fi/mautrix-meta/pkg/messagix"
	"go.mau.fi/mautrix-meta/pkg/messagix/cookies"
	"go.mau.fi/mautrix-meta/pkg/messagix/socket"
	"go.mau.fi/mautrix-meta/pkg/messagix/types"
)

type OutputThread struct {
	ThreadID       int64  `json:"thread_id"`
	Name           string `json:"name"`
	LastActivityMs int64  `json:"last_activity_ms"`
	Snippet        string `json:"snippet"`
	ThreadType     int64  `json:"thread_type"`
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "Usage: %s <cookies_json_path> [max_pages]\n", os.Args[0])
		os.Exit(1)
	}

	cookiesPath := os.Args[1]
	maxPages := 100
	if len(os.Args) >= 3 {
		var err error
		maxPages, err = strconv.Atoi(os.Args[2])
		if err != nil {
			fmt.Fprintf(os.Stderr, "Invalid max_pages: %v\n", err)
			os.Exit(1)
		}
	}

	cookieData, err := os.ReadFile(cookiesPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to read cookies: %v\n", err)
		os.Exit(1)
	}

	var rawCookies map[string]string
	if err := json.Unmarshal(cookieData, &rawCookies); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to parse cookies JSON: %v\n", err)
		os.Exit(1)
	}

	cookieMap := make(map[cookies.MetaCookieName]string)
	for k, v := range rawCookies {
		cookieMap[cookies.MetaCookieName(k)] = v
	}

	logger := zerolog.New(zerolog.NewConsoleWriter(func(w *zerolog.ConsoleWriter) {
		w.Out = os.Stderr
		w.TimeFormat = "15:04:05"
	})).With().Timestamp().Logger().Level(zerolog.WarnLevel)

	c := &cookies.Cookies{Platform: types.Messenger}
	c.UpdateValues(cookieMap)

	client := messagix.NewClient(c, logger, &messagix.Config{})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	fmt.Fprintln(os.Stderr, "Loading messenger page configs...")
	if _, _, err := client.LoadMessagesPage(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "LoadMessagesPage failed: %v\n", err)
		os.Exit(1)
	}

	var (
		allThreads []OutputThread
		mu         sync.Mutex
		done       = make(chan struct{}, 1)
		pageCount  int
		// Pagination state
		minThreadKey      int64 = 0
		minActivityTs     int64 = 9999999999999
		parentThreadKey   int64 = -1
		pendingFetch      bool
		initialSyncDone   bool
	)

	seen := make(map[int64]bool)

	client.SetEventHandler(func(ctx context.Context, evt any) {
		switch e := evt.(type) {
		case *messagix.Event_Ready:
			fmt.Fprintln(os.Stderr, "MQTT ready, fetching initial thread list...")
			go func() {
				cursor := client.GetCursor(1)
				_, err := client.ExecuteTasks(ctx,
					&socket.FetchThreadsTask{
						IsAfter:                    0,
						ParentThreadKey:            -1,
						ReferenceThreadKey:         0,
						ReferenceActivityTimestamp: 9999999999999,
						AdditionalPagesToFetch:     0,
						Cursor:                     cursor,
						SyncGroup:                  1,
					},
				)
				if err != nil {
					fmt.Fprintf(os.Stderr, "ExecuteTasks error: %v\n", err)
				}
				mu.Lock()
				pendingFetch = true
				mu.Unlock()
			}()

		case *messagix.Event_PublishResponse:
			tbl := e.Table
			if tbl == nil {
				return
			}

			mu.Lock()
			if !pendingFetch {
				mu.Unlock()
				return
			}

			// Collect threads from this page
			newCount := 0
			for _, t := range tbl.LSDeleteThenInsertThread {
				if seen[t.ThreadKey] {
					continue
				}
				seen[t.ThreadKey] = true
				newCount++
				allThreads = append(allThreads, OutputThread{
					ThreadID:       t.ThreadKey,
					Name:           t.ThreadName,
					LastActivityMs: t.LastActivityTimestampMs,
					Snippet:        t.Snippet,
					ThreadType:     int64(t.ThreadType),
				})
			}
			pageCount++
			fmt.Fprintf(os.Stderr, "Page %d: got %d new threads (%d total)\n", pageCount, newCount, len(allThreads))

			// Check pagination cursor
			hasMore := false
			for _, r := range tbl.LSUpsertSyncGroupThreadsRange {
				if r.SyncGroup == 1 && r.HasMoreBefore {
					hasMore = true
					minThreadKey = r.MinThreadKey
					minActivityTs = r.MinLastActivityTimestampMS
					parentThreadKey = r.ParentThreadKey
				}
			}

			if !hasMore || (maxPages > 0 && pageCount >= maxPages) || newCount == 0 {
				pendingFetch = false
				mu.Unlock()
				if !hasMore {
					fmt.Fprintln(os.Stderr, "No more threads to fetch.")
				} else if newCount == 0 {
					fmt.Fprintln(os.Stderr, "No new threads in last page, stopping.")
				} else {
					fmt.Fprintf(os.Stderr, "Reached max pages (%d).\n", maxPages)
				}
				select {
				case done <- struct{}{}:
				default:
				}
				return
			}

			initialSyncDone = true
			mu.Unlock()

			// Fetch next page
			go func() {
				cursor := client.GetCursor(1)
				_, err := client.ExecuteTasks(ctx,
					&socket.FetchThreadsTask{
						IsAfter:                    0,
						ParentThreadKey:            parentThreadKey,
						ReferenceThreadKey:         minThreadKey,
						ReferenceActivityTimestamp: minActivityTs,
						AdditionalPagesToFetch:     0,
						Cursor:                     cursor,
						SyncGroup:                  1,
					},
				)
				if err != nil {
					fmt.Fprintf(os.Stderr, "ExecuteTasks error on page %d: %v\n", pageCount+1, err)
					select {
					case done <- struct{}{}:
					default:
					}
				}
			}()
		}
		_ = initialSyncDone
	})

	fmt.Fprintln(os.Stderr, "Connecting to MQTT...")
	if err := client.Connect(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "Connect failed: %v\n", err)
		os.Exit(1)
	}
	defer client.Disconnect()

	select {
	case <-done:
		time.Sleep(500 * time.Millisecond)
	case <-ctx.Done():
		fmt.Fprintln(os.Stderr, "Timeout reached")
	}

	fmt.Fprintf(os.Stderr, "Total: %d threads fetched\n", len(allThreads))
	out, _ := json.Marshal(allThreads)
	fmt.Println(string(out))
}
