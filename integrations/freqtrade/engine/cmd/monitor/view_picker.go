package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type fileInfo struct {
	Name    string
	Path    string
	Size    int64
	ModTime time.Time
}

type filePicker struct {
	files    []fileInfo
	selected int
	dir      string
}

func newFilePicker(dir string) *filePicker {
	fp := &filePicker{dir: dir}
	fp.refresh()
	return fp
}

func (fp *filePicker) refresh() {
	entries, err := os.ReadDir(fp.dir)
	if err != nil {
		return
	}

	fp.files = nil
	for _, e := range entries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".txt" {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		fp.files = append(fp.files, fileInfo{
			Name:    e.Name(),
			Path:    filepath.Join(fp.dir, e.Name()),
			Size:    info.Size(),
			ModTime: info.ModTime(),
		})
	}

	sort.Slice(fp.files, func(i, j int) bool {
		return fp.files[i].ModTime.After(fp.files[j].ModTime)
	})

	if fp.selected >= len(fp.files) {
		fp.selected = 0
	}
}

func (fp *filePicker) up() {
	if fp.selected > 0 {
		fp.selected--
	}
}

func (fp *filePicker) down() {
	if fp.selected < len(fp.files)-1 {
		fp.selected++
	}
}

func (fp *filePicker) selectedPath() string {
	if fp.selected >= len(fp.files) {
		return ""
	}
	return fp.files[fp.selected].Path
}

func renderFilePicker(fp *filePicker, width, height int) string {
	var b strings.Builder

	b.WriteString("\n")
	b.WriteString(styleHeader.Render("  Open Log File") + "\n\n")

	header := fmt.Sprintf("  %-30s %10s  %s", "Name", "Size", "Modified")
	b.WriteString(styleTableHeader.Render(header) + "\n")

	for i, f := range fp.files {
		marker := "  "
		rowStyle := styleTableRow
		if i == fp.selected {
			marker = "> "
			rowStyle = styleTableSelected
		}

		sizeStr := formatSize(f.Size)
		modStr := f.ModTime.Format("Jan 02 15:04")
		live := ""
		if time.Since(f.ModTime) < 60*time.Second {
			live = " " + styleLiveIndicator.Render("●")
		}

		row := fmt.Sprintf("%s%-30s %10s  %s%s", marker, f.Name, sizeStr, modStr, live)
		b.WriteString(rowStyle.Render(row) + "\n")
	}

	b.WriteString("\n" + styleLabel.Render("  ↑/↓ navigate  enter=select  esc=cancel") + "\n")

	return b.String()
}

func formatSize(bytes int64) string {
	if bytes < 1024 {
		return fmt.Sprintf("%dB", bytes)
	}
	if bytes < 1024*1024 {
		return fmt.Sprintf("%.1fK", float64(bytes)/1024)
	}
	return fmt.Sprintf("%.1fM", float64(bytes)/(1024*1024))
}
