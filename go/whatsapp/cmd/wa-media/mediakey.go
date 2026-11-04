package main

import "google.golang.org/protobuf/encoding/protowire"

// parseMediaKey decodes WhatsApp Desktop's ZWAMEDIAITEM.ZMEDIAKEY protobuf.
//
// Field 1 is the 32-byte media key and field 2 the SHA-256 of the encrypted
// file. This was established by downloading a file from the CDN, checking
// its hash against field 2, and decrypting it with field 1.
func parseMediaKey(blob []byte) (mediaKey, encSHA256 []byte, ok bool) {
	for len(blob) > 0 {
		num, typ, n := protowire.ConsumeTag(blob)
		if n < 0 {
			return nil, nil, false
		}
		blob = blob[n:]
		if typ == protowire.BytesType {
			value, m := protowire.ConsumeBytes(blob)
			if m < 0 {
				return nil, nil, false
			}
			switch num {
			case 1:
				mediaKey = value
			case 2:
				encSHA256 = value
			}
			blob = blob[m:]
			continue
		}
		m := protowire.ConsumeFieldValue(num, typ, blob)
		if m < 0 {
			return nil, nil, false
		}
		blob = blob[m:]
	}
	return mediaKey, encSHA256, len(mediaKey) == 32 && len(encSHA256) == 32
}
