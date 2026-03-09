package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

func main() {
	sessionDir := flag.String("session", filepath.Join(os.Getenv("HOME"), ".whatsapp-cli"), "session directory")
	to := flag.String("to", "", "recipient JID (e.g. 41791234567@s.whatsapp.net or 120363...@g.us)")
	message := flag.String("message", "", "message text")
	flag.Parse()

	if *to == "" || *message == "" {
		fmt.Fprintln(os.Stderr, "Usage: wa-send --to JID --message TEXT [--session DIR]")
		os.Exit(1)
	}

	ctx := context.Background()
	dbPath := filepath.Join(*sessionDir, "whatsapp.db")
	if _, err := os.Stat(dbPath); os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "Session not found at %s\n", dbPath)
		os.Exit(1)
	}

	dbLog := waLog.Noop
	container, err := sqlstore.New(ctx, "sqlite3", fmt.Sprintf("file:%s?_foreign_keys=on", dbPath), dbLog)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to open session: %v\n", err)
		os.Exit(1)
	}

	deviceStore, err := container.GetFirstDevice(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to get device: %v\n", err)
		os.Exit(1)
	}

	clientLog := waLog.Noop
	client := whatsmeow.NewClient(deviceStore, clientLog)
	if err := client.Connect(); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to connect: %v\n", err)
		os.Exit(1)
	}
	defer client.Disconnect()

	jid, err := types.ParseJID(*to)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Invalid JID %q: %v\n", *to, err)
		os.Exit(1)
	}

	msg := &waProto.Message{
		Conversation: proto.String(*message),
	}
	_, err = client.SendMessage(ctx, jid, msg)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to send: %v\n", err)
		os.Exit(1)
	}

	fmt.Println("OK")
}
