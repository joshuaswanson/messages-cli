package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"
)

func main() {
	sessionDir := flag.String("session", filepath.Join(os.Getenv("HOME"), ".whatsapp-cli"), "session directory")
	flag.Parse()

	ctx := context.Background()

	// Ensure session directory exists
	if err := os.MkdirAll(*sessionDir, 0700); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to create session directory: %v\n", err)
		os.Exit(1)
	}

	dbPath := filepath.Join(*sessionDir, "whatsapp.db")
	dbLog := waLog.Noop
	container, err := sqlstore.New(ctx, "sqlite3", fmt.Sprintf("file:%s?_foreign_keys=on", dbPath), dbLog)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to open session store: %v\n", err)
		os.Exit(1)
	}

	deviceStore, err := container.GetFirstDevice(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to get device: %v\n", err)
		os.Exit(1)
	}

	clientLog := waLog.Noop
	client := whatsmeow.NewClient(deviceStore, clientLog)

	if client.Store.ID != nil {
		// Already paired
		fmt.Println("Already authenticated.")
		fmt.Printf("Session: %s\n", dbPath)
		// Verify connection works
		if err := client.Connect(); err != nil {
			fmt.Fprintf(os.Stderr, "Warning: connection test failed: %v\n", err)
			os.Exit(1)
		}
		client.Disconnect()
		fmt.Println("Connection verified.")
		return
	}

	// New login: QR pairing
	fmt.Println("Scan the QR code below with WhatsApp on your phone:")
	fmt.Println("Open WhatsApp > Settings > Linked Devices > Link a Device")
	fmt.Println()

	qrChan, _ := client.GetQRChannel(ctx)
	if err := client.Connect(); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to connect: %v\n", err)
		os.Exit(1)
	}
	defer client.Disconnect()

	for evt := range qrChan {
		if evt.Event == "code" {
			qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			fmt.Println("\nWaiting for scan...")
		} else {
			fmt.Printf("Event: %s\n", evt.Event)
		}
	}

	if client.Store.ID != nil {
		fmt.Println("\nAuthenticated successfully!")
		fmt.Printf("Session saved to: %s\n", dbPath)
	} else {
		fmt.Fprintln(os.Stderr, "\nAuthentication failed or timed out.")
		os.Exit(1)
	}
}
