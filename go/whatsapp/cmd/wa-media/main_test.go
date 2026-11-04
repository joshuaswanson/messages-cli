package main

import (
	"bytes"
	"path/filepath"
	"testing"

	"google.golang.org/protobuf/encoding/protowire"
)

func TestParseMediaKey(t *testing.T) {
	key := bytes.Repeat([]byte{1}, 32)
	hash := bytes.Repeat([]byte{2}, 32)
	var blob []byte
	blob = protowire.AppendTag(blob, 1, protowire.BytesType)
	blob = protowire.AppendBytes(blob, key)
	blob = protowire.AppendTag(blob, 2, protowire.BytesType)
	blob = protowire.AppendBytes(blob, hash)
	blob = protowire.AppendTag(blob, 3, protowire.VarintType)
	blob = protowire.AppendVarint(blob, 1700000000)

	gotKey, gotHash, ok := parseMediaKey(blob)
	if !ok || !bytes.Equal(gotKey, key) || !bytes.Equal(gotHash, hash) {
		t.Fatalf("parseMediaKey = %x, %x, %v", gotKey, gotHash, ok)
	}
	if _, _, ok := parseMediaKey(blob[:10]); ok {
		t.Fatal("truncated blob should not parse")
	}
}

func TestDirectPathFromURL(t *testing.T) {
	path, ok := directPathFromURL("https://mmg.whatsapp.net/o1/v/t24/f2/m23/abc.enc?ccb=9-4&oh=01&oe=6A")
	if !ok || path != "/o1/v/t24/f2/m23/abc.enc?ccb=9-4&oh=01&oe=6A" {
		t.Fatalf("got %q, %v", path, ok)
	}
	if _, ok := directPathFromURL("https://example.com/page"); ok {
		t.Fatal("non-WhatsApp URLs must be rejected")
	}
}

func TestChatFilter(t *testing.T) {
	f := newChatFilter("Book Club 2, +41 79 123 45 67")
	cases := []struct {
		jid, name string
		want      bool
	}{
		{"120363@g.us", "Book Club 2", true},
		{"41791234567@s.whatsapp.net", "", true},
		{"2222222@s.whatsapp.net", "Book Club", false},
	}
	for _, c := range cases {
		if got := f.matches(c.jid, c.name); got != c.want {
			t.Errorf("matches(%q, %q) = %v, want %v", c.jid, c.name, got, c.want)
		}
	}
	if !newChatFilter("").matches("x@g.us", "") {
		t.Error("empty filter should match everything")
	}
}

func TestStatusChats(t *testing.T) {
	for _, jid := range []string{"status@broadcast", "41790000000@status", "100000000000001@lid.status"} {
		if !isStatusChat(jid) {
			t.Errorf("%s should be a status chat", jid)
		}
	}
	if isStatusChat("41790000000@s.whatsapp.net") {
		t.Error("a DM is not a status chat")
	}
}

func TestTargetPath(t *testing.T) {
	doc := MediaEntry{ID: "3A/B", ChatJID: "1@g.us", Kind: mediaKinds[3], Filename: "Report.PDF"}
	if got := targetPath("/out", doc); got != filepath.Join("/out", "1@g.us", "3A_B.pdf") {
		t.Errorf("document path = %s", got)
	}
	img := MediaEntry{ID: "X", ChatJID: "1@s.whatsapp.net", Kind: mediaKinds[0]}
	if got := targetPath("/out", img); got != filepath.Join("/out", "1@s.whatsapp.net", "X.jpg") {
		t.Errorf("image path = %s", got)
	}
}
