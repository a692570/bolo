# PROPOSAL: Swift Rewrite of Bolo

## Why Consider a Rewrite?

Bolo is currently ~2000 lines of Python + pyobjc. While functional, the Python approach has inherent limitations:

1. **Startup latency**: Python interpreter + pyobjc bridge adds ~2-3s cold start
2. **Memory footprint**: Python runtime + numpy + sounddevice = ~80MB idle
3. **Distribution complexity**: PyInstaller bundle is ~60MB, requires codesigning workarounds
4. **API friction**: pyobjc bridges are verbose, poorly documented, and occasionally buggy (e.g., NSEvent monitor issues, NSPasteboard edge cases)
5. **Audio stack**: sounddevice (PortAudio wrapper) works but has latency and device-selection quirks
6. **No native feel**: The overlay subprocess model (launch overlay.py as a separate process) is a hack; a native SwiftUI window would be smoother

## What a Swift Version Looks Like

### Architecture

```
BoloApp (SwiftUI menubar app)
├── HotkeyMonitor (CGEventTap, native)
├── AudioRecorder (AVAudioEngine, native)
├── STTClient (URLSession WebSocket, native)
├── TextInjector (CGEvent + NSPasteboard, native)
├── HUDOverlay (SwiftUI NSPanel, native)
└── Preferences (UserDefaults + SwiftUI Settings)
```

### Key Improvements

| Area | Python + pyobjc | Swift |
|------|----------------|-------|
| Startup | ~2-3s | <500ms |
| Memory | ~80MB | ~15-20MB |
| Bundle size | ~60MB (PyInstaller) | ~3-5MB |
| Overlay | subprocess + stdin pipe | Native NSPanel in-process |
| Audio | sounddevice (PortAudio) | AVAudioEngine |
| Hotkey | NSEvent monitor (fragile) | CGEventTap (native, reliable) |
| Distribution | PyInstaller + codesign | Xcode archive + notarize |
| Debugging | print() + /tmp/bolo.log | os_log + Instruments |
| Clipboard | NSPasteboard via bridge | Native NSPasteboard API |

### Effort Estimate

| Component | Lines (Swift) | Time |
|-----------|---------------|------|
| Menubar app scaffold | ~100 | 2h |
| HotkeyMonitor (CGEventTap) | ~150 | 3h |
| AudioRecorder (AVAudioEngine) | ~200 | 4h |
| STTClient (WebSocket) | ~250 | 4h |
| TextInjector (CGEvent + clipboard) | ~150 | 2h |
| HUDOverlay (SwiftUI NSPanel) | ~150 | 3h |
| Preferences UI | ~100 | 2h |
| LLM cleanup pipeline | ~200 | 3h |
| Corrections/vocabulary | ~150 | 2h |
| Testing + polish | — | 4h |
| **Total** | **~1,450** | **~29h** |

### Tradeoffs

**Pros:**
- Native macOS feel (SwiftUI overlay, native hotkey handling)
- Drastically smaller bundle and memory footprint
- Single-process architecture (no overlay subprocess hack)
- Better debugging tools (Instruments, os_log, Swift Concurrency)
- Proper code signing and notarization
- Access to macOS APIs without pyobjc bridge friction
- Swift Concurrency for clean async/await WebSocket + audio handling

**Cons:**
- ~29 hours of development time
- Harder for non-Swift developers to contribute
- Need to maintain two versions during migration
- Some Python ecosystem tools (numpy, requests) are convenient
- Swift's WebSocket ecosystem is less mature than Python's websockets library

### Migration Strategy

If we proceed, the recommended approach is:

1. **Phase 1**: Build a minimal Swift "Bolo Lite" with just hotkey + STT + text injection
2. **Phase 2**: Port overlay, corrections, vocabulary, LLM cleanup
3. **Phase 3**: Add preferences UI, metrics, advanced features
4. **Phase 4**: Parallel run both versions, gather feedback
5. **Phase 5**: Deprecate Python version

Each phase is independently shippable. The Python version stays functional throughout.

### Decision Point

**Recommendation**: Defer for now. The current Python version works well. Revisit when:
- Distribution requirements change (need App Store, smaller bundle)
- Performance becomes a user-facing issue (startup time, memory)
- The overlay subprocess model becomes a maintenance burden
- We want to add native features (e.g., Siri integration, Shortcuts support)

The 29h investment is better spent on features and reliability improvements in the current codebase.
