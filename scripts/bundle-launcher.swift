// Native main executable for Bolo.app (Contents/MacOS/bolo).
//
// The bundle's CFBundleExecutable is this small Mach-O binary, not a shell
// script, so the app presents a native process identity to the system
// while it runs. All the heavy lifting (offline venv setup, single
// instance lock, relaunch-on-crash supervision) stays in the existing
// shell helper, installed as Contents/Resources/bolo-helper. This binary:
//
//   1. locates the helper next to the bundle
//   2. runs it in foreground mode (BOLO_HELPER_FOREGROUND=1), so the
//      supervisor loop is a child of this process instead of a detached
//      nohup, and this process stays alive until the runtime quits cleanly
//   3. propagates signals to the child and exits when it does
//   4. on an interactive launch (Finder double-click, Dock, Launchpad)
//      and on every Finder reopen of the already-running app, writes a
//      bounded open-dashboard request file under ~/.bolo that the
//      runtime's event loop polls, so the user always gets a dashboard or
//      the onboarding window. A login-item startup (the launch
//      AppleEvent carries keyAELaunchedAsLogInItem) and a launch with
//      BOLO_QUIET_STARTUP=1 stay quiet.
//
// The app stays an accessory (LSUIElement): no Dock icon, no windows of
// its own. A second launch is rejected by the helper's existing
// supervisor lock, but this executable still writes the open request
// first, so the second click hands the request to the running instance
// instead of doing nothing. No PID other than the supervised child is
// ever signaled.
//
// Build (also done by scripts/build-dmg.sh):
//   swiftc -target arm64-apple-macos12.0 -O -o bolo-arm64 bundle-launcher.swift
//   swiftc -target x86_64-apple-macos12.0 -O -o bolo-x64 bundle-launcher.swift
//   lipo -create bolo-arm64 bolo-x64 -output bolo
// Foundation, AppKit, Carbon (AE keyword constants) and POSIX only.

import AppKit
import Carbon
import Foundation

private let helperName = "bolo-helper"

/// kAEOpenApplication launch AppleEvent keyword (AERegistry.h) whose
/// presence marks a login-item startup launch.
private let kAELaunchedAsLoginItemKeyword: AEKeyword = AEKeyword(keyAELaunchedAsLogInItem)

/// Directory shared with the runtime for the bounded launch IPC.
private var boloStateDir: String {
    NSHomeDirectory() + "/.bolo"
}

/// The open-dashboard request file. Written atomically (Data's atomic
/// write uses a private temp file plus rename, so concurrent launchers
/// can never collide and the runtime never sees a torn file) and consumed
/// by removing it in the runtime's event loop, so duplicate clicks
/// collapse to a single open.
private var openRequestPath: String {
    boloStateDir + "/open-dashboard.request"
}

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(
        ("[bolo] ERROR: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

private func appResourcesPath() -> String {
    // argv[0]-independent: derive from this executable's path.
    let executablePath = CommandLine.arguments[0]
    let url = URL(fileURLWithPath: executablePath)
        .deletingLastPathComponent()          // Contents/MacOS
        .deletingLastPathComponent()          // Contents
        .appendingPathComponent("Resources")
    return url.path
}

/// Write the open-dashboard request atomically. Overwriting an unconsumed
/// request is fine: the runtime removes the file when it opens the
/// dashboard, so two rapid clicks still open exactly one window.
private func writeOpenRequest() {
    let manager = FileManager.default
    try? manager.createDirectory(
        atPath: boloStateDir, withIntermediateDirectories: true)
    let stamp = String(Date().timeIntervalSince1970).data(using: .utf8)!
    try? stamp.write(
        to: URL(fileURLWithPath: openRequestPath), options: .atomic)
}

// Signal forwarding state. The C handler forwards the signal to the
// supervised child directly (kill() is async-signal-safe and the child PID
// is this process's own child, never an arbitrary PID), so a quit is never
// delayed by runloop scheduling: AppKit's runloop can pause timer
// delivery, and a quit must still reach the helper immediately. The
// supervisor's timer stays responsible for observing the child's exit and
// stopping the runloop, so this process still waits for the child and the
// helper's lock cleanup stays intact.
private var childPIDToSignal: pid_t = 0
private var pendingSignal: Int32 = 0
private func handleSignal(_ signalNumber: Int32) -> Void {
    if childPIDToSignal != 0 {
        kill(childPIDToSignal, signalNumber)
    }
    pendingSignal = signalNumber
}

/// Post one applicationDefined event so the runloop wakes immediately:
/// a bare -stop call only takes effect after the runloop observes an
/// event, and a timer callback alone is not one.
private func postWakeEvent() {
    let wake = NSEvent.otherEvent(
        with: NSEvent.EventType.applicationDefined, location: .zero,
        modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil,
        subtype: 0, data1: 0, data2: 0)
    if let wake = wake {
        NSApplication.shared.postEvent(wake, atStart: false)
    }
}

/// Whether this launch is a login-item startup. The kAEOpenApplication
/// launch AppleEvent carries the keyAELaunchedAsLogInItem parameter only
/// when System Events launched the app as a login item (AERegistry.h),
/// which is read in applicationDidFinishLaunching where the event is
/// still current.
private func launchedAsLoginItem() -> Bool {
    guard let event = NSAppleEventManager.shared().currentAppleEvent else {
        return false
    }
    return event.paramDescriptor(
        forKeyword: kAELaunchedAsLoginItemKeyword) != nil
}

/// Application delegate: handles Finder reopen of the running app and
/// decides the startup request. A reopen always writes the request: the
/// user clicked the app in Finder, so the running runtime must bring its
/// dashboard forward (or open one if it was closed). A login-item or
/// explicitly quiet (BOLO_QUIET_STARTUP=1) startup writes nothing.
final class BoloAppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows: Bool
    ) -> Bool {
        writeOpenRequest()
        return false
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Install forwarding handlers again at the launch callback.
        signal(SIGTERM, handleSignal)
        signal(SIGINT, handleSignal)
        signal(SIGHUP, handleSignal)
        let quiet = ProcessInfo.processInfo.environment["BOLO_QUIET_STARTUP"] == "1"
        if !quiet && !launchedAsLoginItem() {
            // The launch request is written here, where the
            // kAEOpenApplication event is still current: this is the only
            // point where a login-item startup can be told apart from a
            // Finder click. The runtime leaves the file on disk until its
            // event loop is ready to open a window, so a slow first run
            // still honors the click.
            writeOpenRequest()
        }
    }
}

/// Owner of the supervised helper child. Runs on the AppKit main runloop:
/// a repeating timer forwards pending signals and checks the child, an
/// applicationDefined event posted at setup wakes the runloop so the
/// first poll does not wait on a user event, and the child's exit stops
/// the application so the process exits with the child's status, exactly
/// like the previous blocking wait did.
final class BoloSupervisor: NSObject {
    private var child: Process
    private var timer: Timer?
    private let kPollTimerInterval: TimeInterval = 0.2

    init(child: Process) {
        self.child = child
        super.init()
    }

    func start() {
        // Post one applicationDefined event so the runloop wakes even
        // though this accessory app has no user events at startup.
        postWakeEvent()
        let timer = Timer(timeInterval: kPollTimerInterval, target: self,
                          selector: #selector(pollChild),
                          userInfo: nil, repeats: true)
        RunLoop.main.add(timer, forMode: RunLoop.Mode.default)
        self.timer = timer
        NSApplication.shared.run()
        // NSApplication.run returned: the child exited and stop() ran.
    }

    @objc private func pollChild() {
        guard !child.isRunning else { return }
        // Child exited: invalidate the timer and stop the runloop. A bare
        // -stop call only takes effect after the runloop observes an
        // event, so an applicationDefined NSEvent is posted first to wake
        // it; the timer callback alone is not a runloop event and would
        // otherwise leave NSApplication.run waiting. The child's own exit
        // has already completed its lock cleanup, so the parent may exit.
        timer?.invalidate()
        postWakeEvent()
        NSApplication.shared.stop(nil)
    }
}

// --- main -----------------------------------------------------------------

let resources = appResourcesPath()
let helperPath = resources + "/" + helperName
var isDirectory: ObjCBool = false
if !FileManager.default.fileExists(atPath: helperPath, isDirectory: &isDirectory)
    || isDirectory.boolValue {
    fail("helper missing at \(helperPath); reinstall Bolo from the latest DMG")
}

setenv("BOLO_HELPER_FOREGROUND", "1", 1)

// Initialize the accessory application before installing the handlers.
// The delegate also installs them in applicationDidFinishLaunching.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let appDelegate = BoloAppDelegate()
app.delegate = appDelegate

// Do not let a termination signal kill this process outright: the default
// disposition would exit immediately and orphan both the helper and the
// runtime, leaving the supervisor lock behind. The handler forwards the
// signal straight to the supervised child (kill() is async-signal-safe and
// the PID is this process's own child, never an arbitrary one), so a quit
// is never delayed by runloop scheduling.
signal(SIGTERM, handleSignal)
signal(SIGINT, handleSignal)
signal(SIGHUP, handleSignal)

let child = Process()
child.executableURL = URL(fileURLWithPath: helperPath)
child.arguments = []
do {
    try child.run()
} catch {
    fail("could not start the helper at \(helperPath): \(error)")
}
childPIDToSignal = child.processIdentifier

// The startup request is written in applicationDidFinishLaunching, where
// the kAEOpenApplication launch AppleEvent is still current, so a
// login-item startup (keyAELaunchedAsLogInItem) can be told apart from a
// Finder click before anything is requested. The runtime leaves the
// request file on disk until its event loop is ready to open a window, so
// a slow first run still honors the click. Reopens always request.

let supervisor = BoloSupervisor(child: child)
supervisor.start()

let status = Int32(child.terminationStatus)
// Reap synchronously so the loop above can observe the exit.
var waitStatus: Int32 = 0
waitpid(child.processIdentifier, &waitStatus, 0)
exit(status)
