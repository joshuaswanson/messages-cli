package wasession

import signallogger "go.mau.fi/libsignal/logger"

// libsignal logs decryption problems with unrelated incoming messages straight
// to stdout, which garbles the tools' output. whatsmeow reports real failures
// through its own errors and events.
type quietSignalLogger struct{}

func (quietSignalLogger) Debug(string, string)   {}
func (quietSignalLogger) Info(string, string)    {}
func (quietSignalLogger) Warning(string, string) {}
func (quietSignalLogger) Error(string, string)   {}
func (quietSignalLogger) Configure(string)       {}

func init() {
	signallogger.Logger = quietSignalLogger{}
}
