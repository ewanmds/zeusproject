package execution

import (
	"testing"
)

// Compile-time interface satisfaction checks.
var _ Transport = (*SDKTransport)(nil)

func TestSDKTransportSupportsBatch(t *testing.T) {
	st := &SDKTransport{}
	if st.SupportsBatch() {
		t.Error("SDK transport should not support batch operations")
	}
}
