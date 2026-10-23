.PHONY: build run run-with-logs monitor run-monitor test test-race test-v vet lint format clean validate check

GIT_COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null)
LDFLAGS := -ldflags "-X main.gitCommit=$(GIT_COMMIT)"
BIN_DIR := bin

$(BIN_DIR):
	@mkdir -p $(BIN_DIR)

build: $(BIN_DIR)
	go build $(LDFLAGS) -o $(BIN_DIR)/bot ./cmd/bot

run: build
	./$(BIN_DIR)/bot -config config.yaml

run-with-logs: build
	./$(BIN_DIR)/bot -config config.yaml 2>&1 | tee logs/logs.txt

monitor: $(BIN_DIR)
	go build $(LDFLAGS) -o $(BIN_DIR)/monitor ./cmd/monitor

run-monitor: monitor
	./$(BIN_DIR)/monitor

test:
	go test ./... -count=1

test-race:
	go test ./... -count=1 -race

test-v:
	go test ./... -count=1 -v

vet:
	go vet ./...

format:
	go fmt ./...

lint: vet
	@which staticcheck > /dev/null 2>&1 && staticcheck ./... || echo "staticcheck not installed, skipping"

clean:
	rm -rf $(BIN_DIR)

validate: build
	@for f in config.example.yaml; do \
		if [ -f "$$f" ]; then ./$(BIN_DIR)/bot -config "$$f" -validate || exit 1; fi; \
	done

check: vet validate test-race
