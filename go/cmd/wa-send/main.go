// wa-send: send a WhatsApp text message using a paired whatsmeow session.
//
// Usage: wa-send --to JID [--session DIR] < message.txt
//
// The message body is read from stdin so that it does not appear in the
// process list.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"

	"github.com/joshuaswanson/messages-cli/go/internal/wasession"
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

	dbPath := wasession.DBPath(*sessionDir)
	if _, err := os.Stat(dbPath); os.IsNotExist(err) {
		fail("Session not found at %s. Run 'messages auth whatsapp' first.", dbPath)
	}

	ctx := context.Background()
	_, device, err := wasession.Open(ctx, *sessionDir)
	if err != nil {
		fail("%v", err)
	}
	if device.ID == nil {
		fail("Session is not paired. Run 'messages auth whatsapp' first.")
	}

	wasession.UseLatestVersion(ctx)
	client := whatsmeow.NewClient(device, waLog.Noop)
	err = wasession.Connect(ctx, client, connectTimeout)
	if errors.Is(err, wasession.ErrLoggedOut) {
		fail("WhatsApp session was logged out on the phone. Run 'messages auth whatsapp' to pair again.")
	}
	if err != nil {
		fail("%v", err)
	}
	defer client.Disconnect()

	msg := &waProto.Message{Conversation: proto.String(message)}
	if _, err := client.SendMessage(ctx, jid, msg); err != nil {
		fail("Failed to send: %v", err)
	}
	fmt.Println("OK")
}
