// fb-threads-tool: Fetch all Messenger threads via MQTT pagination.
//
// Usage: fb-threads-tool <cookies_json_path> [max_pages] [--e2ee]
//
// Outputs JSON array of threads to stdout. Logs go to stderr.
// Each thread has: thread_id, name, last_activity_ms, snippet, thread_type, folder.
// Fetches from both SyncGroup 1 (inbox) and SyncGroup 95 (E2EE/encrypted).
// Default max_pages is 100 per sync group. Set to 0 for unlimited.
//
// With --e2ee flag, initializes E2EE client to access encrypted threads in SyncGroup 95.
// Device keys are persisted in ~/.config/messages-cli/messenger_e2ee.db.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"sync"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/rs/zerolog"

	"go.mau.fi/mautrix-meta/pkg/messagix"
	"go.mau.fi/mautrix-meta/pkg/messagix/cookies"
	"go.mau.fi/mautrix-meta/pkg/messagix/socket"
	"go.mau.fi/mautrix-meta/pkg/messagix/types"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"
)

type OutputThread struct {
	ThreadID       int64  `json:"thread_id"`
	Name           string `json:"name"`
	LastActivityMs int64  `json:"last_activity_ms"`
	Snippet        string `json:"snippet"`
	ThreadType     int64  `json:"thread_type"`
	Folder         string `json:"folder"`
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "Usage: %s <cookies_json_path> [max_pages] [--e2ee]\n", os.Args[0])
		os.Exit(1)
	}

	cookiesPath := os.Args[1]
	maxPages := 100
	enableE2EE := slices.Contains(os.Args, "--e2ee")

	// Parse max_pages from positional args (skip --e2ee)
	for _, arg := range os.Args[2:] {
		if arg == "--e2ee" {
			continue
		}
		var err error
		maxPages, err = strconv.Atoi(arg)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Invalid max_pages: %v\n", err)
			os.Exit(1)
		}
		break
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
	currentUser, initialTable, err := client.LoadMessagesPage(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "LoadMessagesPage failed: %v\n", err)
		os.Exit(1)
	}

	// Initialize E2EE if requested
	if enableE2EE {
		fbid := currentUser.GetFBID()
		fmt.Fprintf(os.Stderr, "Initializing E2EE for fbid=%d...\n", fbid)

		configDir := filepath.Join(os.Getenv("HOME"), ".config", "messages-cli")
		if err := os.MkdirAll(configDir, 0700); err != nil {
			fmt.Fprintf(os.Stderr, "Failed to create config dir: %v\n", err)
			os.Exit(1)
		}

		dbPath := filepath.Join(configDir, "messenger_e2ee.db")
		dbURI := fmt.Sprintf("file:%s?_foreign_keys=on", dbPath)

		container, err := sqlstore.New(ctx, "sqlite3", dbURI,
			waLog.Zerolog(logger.With().Str("component", "sqlstore").Logger()))
		if err != nil {
			fmt.Fprintf(os.Stderr, "Failed to init device store: %v\n", err)
			os.Exit(1)
		}

		// Try to load existing device, or create new one
		device, err := container.GetFirstDevice(ctx)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Failed to get device: %v\n", err)
			os.Exit(1)
		}

		client.SetDevice(device)

		isNew := device.ID == nil
		if isNew {
			fmt.Fprintln(os.Stderr, "Registering new E2EE device...")
			if err := client.RegisterE2EE(ctx, fbid); err != nil {
				fmt.Fprintf(os.Stderr, "E2EE registration failed: %v\n", err)
				os.Exit(1)
			}
			if err := device.Save(ctx); err != nil {
				fmt.Fprintf(os.Stderr, "Failed to save device: %v\n", err)
				os.Exit(1)
			}
			fmt.Fprintf(os.Stderr, "E2EE device registered: %s\n", device.ID)
		} else {
			fmt.Fprintf(os.Stderr, "Using existing E2EE device: %s\n", device.ID)
		}

		e2eeClient, err := client.PrepareE2EEClient()
		if err != nil {
			fmt.Fprintf(os.Stderr, "PrepareE2EEClient failed: %v\n", err)
			os.Exit(1)
		}

		fmt.Fprintln(os.Stderr, "Connecting E2EE client...")
		if err := e2eeClient.Connect(); err != nil {
			fmt.Fprintf(os.Stderr, "E2EE Connect failed: %v\n", err)
			os.Exit(1)
		}
		defer e2eeClient.Disconnect()
		fmt.Fprintln(os.Stderr, "E2EE client connected.")

		// Close DB when done
		defer func() {
			_ = container.Close()
		}()

		// Give E2EE client a moment to sync
		time.Sleep(2 * time.Second)
	}

	var (
		allThreads []OutputThread
		mu         sync.Mutex
		done       = make(chan struct{}, 1)
		sg1Pages   int
		sg95Pages  int
		sg1Done    bool
		sg95Done   bool
		pendingFetch bool
	)

	seen := make(map[int64]bool)

	// Collect threads from the initial Lightspeed sync (includes E2EE threads)
	if initialTable != nil {
		for _, t := range initialTable.LSDeleteThenInsertThread {
			if seen[t.ThreadKey] {
				continue
			}
			seen[t.ThreadKey] = true
			allThreads = append(allThreads, OutputThread{
				ThreadID:       t.ThreadKey,
				Name:           t.ThreadName,
				LastActivityMs: t.LastActivityTimestampMs,
				Snippet:        t.Snippet,
				ThreadType:     int64(t.ThreadType),
				Folder:         t.FolderName,
			})
		}
		fmt.Fprintf(os.Stderr, "Initial sync: %d threads\n", len(allThreads))
	}

	client.SetEventHandler(func(ctx context.Context, evt any) {
		switch e := evt.(type) {
		case *messagix.Event_Ready:
			fmt.Fprintln(os.Stderr, "MQTT ready, fetching thread lists (SyncGroup 1 + 95)...")
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
					&socket.FetchThreadsTask{
						IsAfter:                    0,
						ParentThreadKey:            -1,
						ReferenceThreadKey:         0,
						ReferenceActivityTimestamp: 9999999999999,
						AdditionalPagesToFetch:     0,
						SyncGroup:                  95,
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
					Folder:         t.FolderName,
				})
			}

			var nextTasks []socket.Task
			for _, r := range tbl.LSUpsertSyncGroupThreadsRange {
				if r.SyncGroup == 1 && r.HasMoreBefore && !sg1Done {
					sg1Pages++
					if maxPages > 0 && sg1Pages >= maxPages {
						sg1Done = true
						fmt.Fprintf(os.Stderr, "SyncGroup 1: reached max pages (%d)\n", maxPages)
					} else {
						cursor := client.GetCursor(1)
						nextTasks = append(nextTasks, &socket.FetchThreadsTask{
							IsAfter:                    0,
							ParentThreadKey:            r.ParentThreadKey,
							ReferenceThreadKey:         r.MinThreadKey,
							ReferenceActivityTimestamp: r.MinLastActivityTimestampMS,
							AdditionalPagesToFetch:     0,
							Cursor:                     cursor,
							SyncGroup:                  1,
						})
					}
				} else if r.SyncGroup == 1 && !r.HasMoreBefore {
					sg1Done = true
				}

				if r.SyncGroup == 95 && r.HasMoreBefore && !sg95Done {
					sg95Pages++
					if maxPages > 0 && sg95Pages >= maxPages {
						sg95Done = true
						fmt.Fprintf(os.Stderr, "SyncGroup 95: reached max pages (%d)\n", maxPages)
					} else {
						nextTasks = append(nextTasks, &socket.FetchThreadsTask{
							IsAfter:                    0,
							ParentThreadKey:            r.ParentThreadKey,
							ReferenceThreadKey:         r.MinThreadKey,
							ReferenceActivityTimestamp: r.MinLastActivityTimestampMS,
							AdditionalPagesToFetch:     0,
							SyncGroup:                  95,
						})
					}
				} else if r.SyncGroup == 95 && !r.HasMoreBefore {
					sg95Done = true
				}
			}

			fmt.Fprintf(os.Stderr, "Got %d new threads (%d total), sg1_pages=%d sg95_pages=%d\n",
				newCount, len(allThreads), sg1Pages, sg95Pages)

			if len(tbl.LSUpsertSyncGroupThreadsRange) == 0 {
				if !sg1Done {
					sg1Done = true
				}
				if !sg95Done {
					sg95Done = true
				}
			}

			if sg1Done && sg95Done || (len(nextTasks) == 0 && newCount == 0) {
				pendingFetch = false
				mu.Unlock()
				fmt.Fprintln(os.Stderr, "All sync groups exhausted.")
				select {
				case done <- struct{}{}:
				default:
				}
				return
			}

			mu.Unlock()

			if len(nextTasks) > 0 {
				go func() {
					_, err := client.ExecuteTasks(ctx, nextTasks...)
					if err != nil {
						fmt.Fprintf(os.Stderr, "ExecuteTasks error: %v\n", err)
						select {
						case done <- struct{}{}:
						default:
						}
					}
				}()
			}
		}
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

// Ensure sql import is used (required for sqlite3 driver registration).
var _ = sql.Drivers
