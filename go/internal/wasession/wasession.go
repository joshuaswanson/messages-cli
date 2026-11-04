// Package wasession opens the whatsmeow session store and connects clients.
package wasession

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// ErrLoggedOut means the linked device was removed on the phone.
var ErrLoggedOut = errors.New("WhatsApp session was logged out")

// UseLatestVersion makes the client announce the current WhatsApp Web version.
//
// WhatsApp rejects clients whose built-in version is too old (pairing fails
// with err-client-outdated), so the version is fetched on every run. If the
// fetch fails, the version compiled into whatsmeow is used.
func UseLatestVersion(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	latest, err := whatsmeow.GetLatestVersion(ctx, &http.Client{})
	if err != nil {
		fmt.Fprintf(os.Stderr, "Warning: could not fetch the current WhatsApp Web version (%v). Using %s.\n", err, store.GetWAVersion())
		return
	}
	store.SetWAVersion(*latest)
}

func DBPath(sessionDir string) string {
	return filepath.Join(sessionDir, "whatsapp.db")
}

func Open(ctx context.Context, sessionDir string) (*sqlstore.Container, *store.Device, error) {
	dsn := fmt.Sprintf("file:%s?_foreign_keys=on", DBPath(sessionDir))
	container, err := sqlstore.New(ctx, "sqlite3", dsn, waLog.Noop)
	if err != nil {
		return nil, nil, fmt.Errorf("open session store: %w", err)
	}
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return nil, nil, fmt.Errorf("get device: %w", err)
	}
	return container, device, nil
}

// Connect connects a paired client and waits until WhatsApp accepts the session.
//
// client.Connect only opens the websocket, so a removed device would look
// connected. On logout this deletes the device from the store before
// returning ErrLoggedOut. whatsmeow also deletes it, but from a goroutine
// that a caller exiting right away would cut short.
func Connect(ctx context.Context, client *whatsmeow.Client, timeout time.Duration) error {
	connected := make(chan struct{}, 1)
	loggedOut := make(chan struct{}, 1)
	handler := client.AddEventHandler(func(evt any) {
		var ch chan struct{}
		switch evt.(type) {
		case *events.Connected:
			ch = connected
		case *events.LoggedOut:
			ch = loggedOut
		default:
			return
		}
		select {
		case ch <- struct{}{}:
		default:
		}
	})
	defer client.RemoveEventHandler(handler)

	if err := client.Connect(); err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	select {
	case <-connected:
		return nil
	case <-loggedOut:
		client.Disconnect()
		if err := client.Store.Delete(ctx); err != nil {
			return fmt.Errorf("%w (and failed to delete the stale device: %v)", ErrLoggedOut, err)
		}
		return ErrLoggedOut
	case <-time.After(timeout):
		client.Disconnect()
		return errors.New("timed out waiting for WhatsApp connection")
	}
}
