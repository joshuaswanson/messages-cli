// wa-auth: pair a whatsmeow session with a phone by scanning a QR code.
//
// Usage: wa-auth [--session DIR]
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/mdp/qrterminal"
	"go.mau.fi/whatsmeow"
	waLog "go.mau.fi/whatsmeow/util/log"

	"github.com/joshuaswanson/messages-cli/go/whatsapp/internal/wasession"
)

const connectTimeout = 20 * time.Second

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}

func main() {
	sessionDir := flag.String("session", filepath.Join(os.Getenv("HOME"), ".whatsapp-cli"), "session directory")
	flag.Parse()

	ctx := context.Background()
	if err := os.MkdirAll(*sessionDir, 0o700); err != nil {
		fail("Failed to create session directory: %v", err)
	}
	dbPath := wasession.DBPath(*sessionDir)
	container, device, err := wasession.Open(ctx, *sessionDir)
	if err != nil {
		fail("%v", err)
	}
	wasession.UseLatestVersion(ctx)
	client := whatsmeow.NewClient(device, waLog.Noop)

	if device.ID != nil {
		err := wasession.Connect(ctx, client, connectTimeout)
		switch {
		case err == nil:
			client.Disconnect()
			fmt.Println("Already authenticated. Connection verified.")
			fmt.Printf("Session: %s\n", dbPath)
			return
		case errors.Is(err, wasession.ErrLoggedOut):
			fmt.Println("The previous session was logged out on the phone. Pairing again.")
			client = whatsmeow.NewClient(container.NewDevice(), waLog.Noop)
		default:
			fail("Connection test failed: %v", err)
		}
	}

	fmt.Println("Scan the QR code below with WhatsApp on your phone:")
	fmt.Println("Open WhatsApp > Settings > Linked Devices > Link a Device")
	fmt.Println()

	qrChan, err := client.GetQRChannel(ctx)
	if err != nil {
		fail("Failed to start pairing: %v", err)
	}
	if err := client.Connect(); err != nil {
		fail("Failed to connect: %v", err)
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

	if client.Store.ID == nil {
		fail("\nAuthentication failed or timed out.")
	}
	fmt.Println("\nAuthenticated successfully!")
	fmt.Printf("Session saved to: %s\n", dbPath)
}
