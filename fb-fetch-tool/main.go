// fb-fetch-tool: Fetch older messages from a Messenger thread via MQTT.
//
// Usage: fb-fetch-tool <cookies_json_path> <thread_id> <ref_timestamp_ms> <ref_message_id>
//
// Outputs JSON array of messages to stdout. Logs go to stderr.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/rs/zerolog"

	"go.mau.fi/mautrix-meta/pkg/messagix"
	"go.mau.fi/mautrix-meta/pkg/messagix/cookies"
	"go.mau.fi/mautrix-meta/pkg/messagix/socket"
	"go.mau.fi/mautrix-meta/pkg/messagix/types"
)

type OutputMessage struct {
	Text        string `json:"text"`
	TimestampMs int64  `json:"timestamp_ms"`
	SenderID    int64  `json:"sender_id"`
	MessageID   string `json:"message_id"`
}

func main() {
	if len(os.Args) < 5 {
		fmt.Fprintf(os.Stderr, "Usage: %s <cookies_json_path> <thread_id> <ref_timestamp_ms> <ref_message_id>\n", os.Args[0])
		os.Exit(1)
	}

	cookiesPath := os.Args[1]
	threadID, err := strconv.ParseInt(os.Args[2], 10, 64)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Invalid thread_id: %v\n", err)
		os.Exit(1)
	}
	refTimestampMs, err := strconv.ParseInt(os.Args[3], 10, 64)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Invalid ref_timestamp_ms: %v\n", err)
		os.Exit(1)
	}
	refMessageId := os.Args[4]

	// Read cookies
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

	// Convert to MetaCookieName map
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

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	// Load the messenger page to get configs
	fmt.Fprintln(os.Stderr, "Loading messenger page configs...")
	if _, _, err := client.LoadMessagesPage(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "LoadMessagesPage failed: %v\n", err)
		os.Exit(1)
	}

	// Set up event handler to capture messages
	done := make(chan struct{}, 1)
	var messages []OutputMessage

	client.SetEventHandler(func(ctx context.Context, evt any) {
		switch e := evt.(type) {
		case *messagix.Event_Ready:
			fmt.Fprintln(os.Stderr, "MQTT ready, sending FetchMessagesTask...")
			go func() {
				cursor := client.GetCursor(1)
				_, err := client.ExecuteTasks(ctx,
					&socket.FetchMessagesTask{
						ThreadKey:            threadID,
						Direction:            0,
						ReferenceTimestampMs: refTimestampMs,
						ReferenceMessageId:   refMessageId,
						SyncGroup:            1,
						Cursor:               cursor,
					},
				)
				if err != nil {
					fmt.Fprintf(os.Stderr, "ExecuteTasks error: %v\n", err)
				}
			}()

		case *messagix.Event_PublishResponse:
			tbl := e.Table
			if tbl == nil {
				return
			}
			upsertMap, _ := tbl.WrapMessages()
			threadMsgs, ok := upsertMap[threadID]
			if !ok || threadMsgs == nil {
				return
			}
			for _, wm := range threadMsgs.Messages {
				if wm.Text == "" {
					continue
				}
				messages = append(messages, OutputMessage{
					Text:        wm.Text,
					TimestampMs: wm.TimestampMs,
					SenderID:    wm.SenderId,
					MessageID:   wm.MessageId,
				})
			}
			if len(messages) > 0 {
				select {
				case done <- struct{}{}:
				default:
				}
			}
		}
	})

	// Connect via MQTT WebSocket
	fmt.Fprintln(os.Stderr, "Connecting to MQTT...")
	if err := client.Connect(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "Connect failed: %v\n", err)
		os.Exit(1)
	}
	defer client.Disconnect()

	// Wait for messages or timeout
	select {
	case <-done:
		time.Sleep(500 * time.Millisecond)
	case <-time.After(30 * time.Second):
		fmt.Fprintln(os.Stderr, "Timeout waiting for messages")
		os.Exit(1)
	}

	// Output
	out, _ := json.Marshal(messages)
	fmt.Println(string(out))
}
