from messages_cli import whatsapp_db


def test_downloaded_media_lookup_matches_wa_media_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(whatsapp_db, "WA_MEDIA_DIR", tmp_path)
    folder = tmp_path / "120363@g.us"
    folder.mkdir()
    (folder / "3A_B.jpg").write_bytes(b"x")
    assert whatsapp_db._downloaded_media("120363@g.us", "3A/B") == folder / "3A_B.jpg"
    assert whatsapp_db._downloaded_media("120363@g.us", "missing") is None
    assert whatsapp_db._downloaded_media("120363@g.us", None) is None
