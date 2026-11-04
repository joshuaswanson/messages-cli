// wa-send: send a WhatsApp text message using a paired whatsmeow session.
//
// Usage: wa-send --to JID [--session DIR] < message.txt
//
// The message body is read from stdin so that it does not appear in the
// process list.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

const connectTimeout = 20 * time.Second

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}

func main() {
	sessionDir := flag.String("session", filepath.Join(os.Getenv("HOME"), ".whatsapp-cli"), "session directory")
	to := flag.String("to", "", "recipient JID (e.g. 41791234567@s.whatsapp.net or 120363...@g.us)")
	flag.Parse()

	if *to == "" {
		fail("Usage: wa-send --to JID [--session DIR] < message.txt")
	}
	body, err := io.ReadAll(os.Stdin)
	if err != nil {
		fail("Failed to read message from stdin: %v", err)
	}
	message := strings.TrimRight(string(body), "\n")
	if message == "" {
		fail("Message is empty")
	}

	jid, err := types.ParseJID(*to)
	if err != nil {
		fail("Invalid JID %q: %v", *to, err)
	}
	switch {
	case jid.User == "" || strings.ContainsAny(jid.User, " \t\n"):
		fail("Invalid JID %q: missing or malformed user part", *to)
	case jid.Server != types.DefaultUserServer && jid.Server != types.GroupServer && jid.Server != types.HiddenUserServer:
		fail("Invalid JID %q: server must be %s, %s, or %s", *to, types.DefaultUserServer, types.GroupServer, types.HiddenUserServer)
	}

	dbPath := filepath.Join(*sessionDir, "whatsapp.db")
	if _, err := os.Stat(dbPath); os.IsNotExist(err) {
		fail("Session not found at %s. Run 'messages auth whatsapp' first.", dbPath)
	}

	ctx := context.Background()
	container, err := sqlstore.New(ctx, "sqlite3", fmt.Sprintf("file:%s?_foreign_keys=on", dbPath), waLog.Noop)
	if err != nil {
		fail("Failed to open session: %v", err)
	}
	deviceStore, err := container.GetFirstDevice(ctx)
	if err != nil {
		fail("Failed to get device: %v", err)
	}
	if deviceStore.ID == nil {
		fail("Session is not paired. Run 'messages auth whatsapp' first.")
	}

	client := whatsmeow.NewClient(deviceStore, waLog.Noop)
	connected := make(chan struct{}, 1)
	loggedOut := make(chan struct{}, 1)
	client.AddEventHandler(func(evt any) {
		switch evt.(type) {
		case *events.Connected:
			select {
			case connected <- struct{}{}:
			default:
			}
		case *events.LoggedOut:
			select {
			case loggedOut <- struct{}{}:
			default:
			}
		}
	})

	if err := client.Connect(); err != nil {
		fail("Failed to connect: %v", err)
	}
	defer client.Disconnect()

	select {
	case <-connected:
	case <-loggedOut:
		fail("Session was logged out. Run 'messages auth whatsapp' again.")
	case <-time.After(connectTimeout):
		fail("Timed out waiting for WhatsApp connection")
	}

	msg := &waProto.Message{Conversation: proto.String(message)}
	if _, err := client.SendMessage(ctx, jid, msg); err != nil {
		fail("Failed to send: %v", err)
	}
	fmt.Println("OK")
}
