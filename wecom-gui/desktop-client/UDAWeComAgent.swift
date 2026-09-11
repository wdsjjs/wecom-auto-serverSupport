import AppKit
import ApplicationServices

private struct DesktopAccess {
    let accessibility: Bool
    let screenCapture: Bool

    static func current() -> DesktopAccess {
        DesktopAccess(accessibility: AXIsProcessTrusted(), screenCapture: CGPreflightScreenCaptureAccess())
    }

    var issue: String? {
        if !accessibility { return "需要重新授权无障碍" }
        if !screenCapture { return "需要屏幕录制权限" }
        return nil
    }
}

private enum JsonValue: Decodable, CustomStringConvertible {
    case string(String), number(Double), bool(Bool), null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let item = try? value.decode(Bool.self) { self = .bool(item) }
        else if let item = try? value.decode(Double.self) { self = .number(item) }
        else { self = .string(try value.decode(String.self)) }
    }

    var description: String {
        switch self {
        case .string(let value): return value
        case .number(let value): return String(format: "%.0f", value)
        case .bool(let value): return value ? "true" : "false"
        case .null: return ""
        }
    }
}

private struct RuntimeProcess: Decodable {
    let process: String
    let status: String
    let phase: String
    let conversation_label: String
    let direction: String
    let rationale: String
    let metrics: [String: JsonValue]
    let error_code: String
    let updated_at: Double
}

private struct RuntimeEvent: Decodable {
    let process: String
    let status: String
    let phase: String
    let conversation_key: String
    let conversation_label: String
    let direction: String
    let rationale: String
    let metrics: [String: JsonValue]
    let error_code: String
    let occurred_at: Double
}

private struct RuntimeSnapshot: Decodable {
    let states: [RuntimeProcess]
    let events: [RuntimeEvent]
}

private final class FloatingStatusPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

private final class FloatingDashboardView: NSView {
    private var snapshot = RuntimeSnapshot(states: [], events: [])
    private var pulse: CGFloat = 0
    private var animationTimer: Timer?
    var controlNotice = "" { didSet { needsDisplay = true } }
    var onDetails: (() -> Void)?
    var onLogs: (() -> Void)?
    var onWorkbench: (() -> Void)?
    var onStartWeCom: (() -> Void)?
    var onStopWeCom: (() -> Void)?
    var onStartEdge: (() -> Void)?
    var onStopEdge: (() -> Void)?

    private let startWeComRect = NSRect(x: 20, y: 90, width: 102, height: 30)
    private let stopWeComRect = NSRect(x: 129, y: 90, width: 102, height: 30)
    private let startEdgeRect = NSRect(x: 238, y: 90, width: 102, height: 30)
    private let stopEdgeRect = NSRect(x: 20, y: 52, width: 102, height: 30)
    private let detailRect = NSRect(x: 129, y: 52, width: 102, height: 30)
    private let logsRect = NSRect(x: 238, y: 52, width: 102, height: 30)
    private let workbenchRect = NSRect(x: 20, y: 14, width: 320, height: 30)

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        animationTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 24.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.pulse += 0.13
            self.needsDisplay = true
        }
    }

    required init?(coder: NSCoder) { nil }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    deinit { animationTimer?.invalidate() }

    func update(_ value: RuntimeSnapshot) {
        snapshot = value
        needsDisplay = true
    }

    private func state(_ process: String) -> RuntimeProcess? {
        snapshot.states.first { $0.process == process }
    }

    private func centralCommandState() -> RuntimeProcess {
        guard let edge = state("edge_channel") else {
            return RuntimeProcess(process: "central_dispatch", status: "stopped", phase: "edge_not_connected", conversation_label: "", direction: "", rationale: "", metrics: [:], error_code: "", updated_at: 0)
        }
        if edge.phase == "waiting_for_command" {
            return RuntimeProcess(process: "central_dispatch", status: "waiting", phase: "long_polling", conversation_label: edge.conversation_label, direction: "", rationale: "", metrics: edge.metrics, error_code: edge.error_code, updated_at: edge.updated_at)
        }
        return RuntimeProcess(process: "central_dispatch", status: edge.status, phase: edge.phase, conversation_label: edge.conversation_label, direction: edge.direction, rationale: edge.rationale, metrics: edge.metrics, error_code: edge.error_code, updated_at: edge.updated_at)
    }

    private func color(for status: String?) -> NSColor {
        switch status {
        case "failed": return NSColor.systemRed
        case "waiting", "retrying": return NSColor.systemYellow
        case "running", "idle": return NSColor.systemTeal
        default: return NSColor(calibratedWhite: 0.48, alpha: 1)
        }
    }

    private func statusText(_ value: String?) -> String {
        ["idle": "正常", "running": "运行中", "waiting": "等待确认", "retrying": "重试中", "failed": "需要处理", "stopped": "已停止"][value ?? ""] ?? "未启动"
    }

    private func shortProcess(_ value: String) -> String {
        value == "central_dispatch" ? "中台指令" : "边缘通道"
    }

    private func shortened(_ value: String, limit: Int) -> String {
        guard value.count > limit else { return value }
        return String(value.prefix(limit - 1)) + "..."
    }

    private func drawText(_ value: String, at point: NSPoint, font: NSFont, color: NSColor) {
        value.draw(at: point, withAttributes: [.font: font, .foregroundColor: color])
    }

    private func drawRounded(_ rect: NSRect, radius: CGFloat, color: NSColor) {
        color.setFill()
        NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let bounds = self.bounds
        let backdrop = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 18, yRadius: 18)
        NSColor(calibratedRed: 0.055, green: 0.071, blue: 0.095, alpha: 1).setFill()
        backdrop.fill()
        NSColor(calibratedWhite: 1, alpha: 0.12).setStroke()
        backdrop.lineWidth = 1
        backdrop.stroke()

        let allStatuses = [state("edge_channel")?.status].compactMap { $0 }
        let overall: String = allStatuses.contains("failed") ? "需要处理" : (allStatuses.contains("waiting") || allStatuses.contains("retrying")) ? "等待确认" : (allStatuses.contains("running") || allStatuses.contains("idle")) ? "稳定运行" : "服务未启动"
        let overallColor = color(for: allStatuses.contains("failed") ? "failed" : (allStatuses.contains("waiting") || allStatuses.contains("retrying")) ? "waiting" : (allStatuses.contains("running") || allStatuses.contains("idle")) ? "running" : "stopped")
        let breathing = 0.50 + 0.50 * sin(pulse)
        let center = NSPoint(x: 31, y: bounds.height - 34)
        overallColor.withAlphaComponent(0.10 + breathing * 0.20).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - 14 - breathing * 4, y: center.y - 14 - breathing * 4, width: 28 + breathing * 8, height: 28 + breathing * 8)).fill()
        overallColor.setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - 6, y: center.y - 6, width: 12, height: 12)).fill()
        drawText("UDA  WECOM AGENT", at: NSPoint(x: 54, y: bounds.height - 31), font: .monospacedSystemFont(ofSize: 11, weight: .medium), color: NSColor(calibratedWhite: 0.72, alpha: 1))
        drawText(overall, at: NSPoint(x: 54, y: bounds.height - 51), font: .systemFont(ofSize: 18, weight: .semibold), color: .white)
        drawText("LOCAL CONTROL PLANE", at: NSPoint(x: 232, y: bounds.height - 29), font: .monospacedSystemFont(ofSize: 9, weight: .medium), color: overallColor)

        drawServiceCard(state("edge_channel"), title: "MAC EDGE CHANNEL", frame: NSRect(x: 18, y: bounds.height - 150, width: bounds.width - 36, height: 68))
        drawServiceCard(centralCommandState(), title: "CENTRAL COMMANDS", frame: NSRect(x: 18, y: bounds.height - 226, width: bounds.width - 36, height: 68))

        drawText("FLOW TRACE", at: NSPoint(x: 20, y: bounds.height - 254), font: .monospacedSystemFont(ofSize: 10, weight: .medium), color: NSColor(calibratedWhite: 0.61, alpha: 1))
        let events = Array(snapshot.events.prefix(3))
        if events.isEmpty {
            drawText("等待本机进程写入活动...", at: NSPoint(x: 36, y: bounds.height - 280), font: .systemFont(ofSize: 12), color: NSColor(calibratedWhite: 0.57, alpha: 1))
        } else {
            for (index, event) in events.enumerated() {
                let y = bounds.height - 279 - CGFloat(index) * 33
                let eventColor = color(for: event.status)
                eventColor.setFill()
                NSBezierPath(ovalIn: NSRect(x: 21, y: y + 2, width: 7, height: 7)).fill()
                if index < events.count - 1 {
                    NSColor(calibratedWhite: 1, alpha: 0.15).setStroke()
                    let path = NSBezierPath(); path.move(to: NSPoint(x: 24.5, y: y)); path.line(to: NSPoint(x: 24.5, y: y - 24)); path.lineWidth = 1; path.stroke()
                }
                drawText("\(shortProcess(event.process))  /  \(shortened(event.phase, limit: 24))", at: NSPoint(x: 39, y: y), font: .systemFont(ofSize: 12, weight: .medium), color: NSColor(calibratedWhite: 0.90, alpha: 1))
                let detail = event.conversation_label.isEmpty ? (event.direction.isEmpty ? "本机状态更新" : event.direction) : event.conversation_label
                drawText(shortened(detail, limit: 34), at: NSPoint(x: 39, y: y - 14), font: .systemFont(ofSize: 10), color: NSColor(calibratedWhite: 0.58, alpha: 1))
            }
        }

        drawText(shortened(controlNotice, limit: 29), at: NSPoint(x: 20, y: 129), font: .systemFont(ofSize: 11), color: .systemYellow)
        drawFooterButton("启动企微", rect: startWeComRect, icon: "play.fill")
        drawFooterButton("停止企微", rect: stopWeComRect, icon: "stop.fill")
        drawFooterButton("启动边缘", rect: startEdgeRect, icon: "bolt.fill")
        drawFooterButton("停止边缘", rect: stopEdgeRect, icon: "bolt.slash.fill")
        drawFooterButton("详情", rect: detailRect, icon: "list.bullet")
        drawFooterButton("日志", rect: logsRect, icon: "doc.text")
        drawFooterButton("中台", rect: workbenchRect, icon: "arrow.up.right.square")
    }

    private func drawServiceCard(_ state: RuntimeProcess?, title: String, frame: NSRect) {
        drawRounded(frame, radius: 10, color: NSColor(calibratedWhite: 1, alpha: 0.055))
        let status = state?.status
        let stateColor = color(for: status)
        drawText(title, at: NSPoint(x: frame.minX + 13, y: frame.maxY - 22), font: .monospacedSystemFont(ofSize: 10, weight: .medium), color: NSColor(calibratedWhite: 0.55, alpha: 1))
        drawText(statusText(status), at: NSPoint(x: frame.minX + 13, y: frame.minY + 13), font: .systemFont(ofSize: 15, weight: .semibold), color: .white)
        let pill = NSRect(x: frame.maxX - 84, y: frame.maxY - 28, width: 70, height: 18)
        drawRounded(pill, radius: 9, color: stateColor.withAlphaComponent(0.18))
        drawText(statusText(status), at: NSPoint(x: pill.minX + 10, y: pill.minY + 3), font: .systemFont(ofSize: 10, weight: .medium), color: stateColor)
        let phase = state?.phase.isEmpty == false ? state!.phase : "未启动"
        let detail = state?.conversation_label.isEmpty == false ? state!.conversation_label : (state?.error_code.isEmpty == false ? state!.error_code : "等待下一步流转")
        drawText(shortened(phase, limit: 24), at: NSPoint(x: frame.minX + 116, y: frame.minY + 29), font: .systemFont(ofSize: 11, weight: .medium), color: NSColor(calibratedWhite: 0.82, alpha: 1))
        drawText(shortened(detail, limit: 30), at: NSPoint(x: frame.minX + 116, y: frame.minY + 13), font: .systemFont(ofSize: 10), color: NSColor(calibratedWhite: 0.54, alpha: 1))
    }

    private func drawFooterButton(_ title: String, rect: NSRect, icon: String) {
        drawRounded(rect, radius: 8, color: NSColor(calibratedWhite: 1, alpha: 0.08))
        if let image = NSImage(systemSymbolName: icon, accessibilityDescription: title) {
            image.withSymbolConfiguration(.init(pointSize: 11, weight: .medium))?.draw(in: NSRect(x: rect.minX + 13, y: rect.minY + 9, width: 12, height: 12))
        }
        drawText(title, at: NSPoint(x: rect.minX + 33, y: rect.minY + 8), font: .systemFont(ofSize: 11, weight: .medium), color: NSColor(calibratedWhite: 0.83, alpha: 1))
    }

    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        if startWeComRect.contains(point) { onStartWeCom?() }
        else if stopWeComRect.contains(point) { onStopWeCom?() }
        else if startEdgeRect.contains(point) { onStartEdge?() }
        else if stopEdgeRect.contains(point) { onStopEdge?() }
        else if detailRect.contains(point) { onDetails?() }
        else if logsRect.contains(point) { onLogs?() }
        else if workbenchRect.contains(point) { onWorkbench?() }
    }
}

private final class AgentApp: NSObject, NSApplicationDelegate {
    private let root: String
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let summary = NSMenuItem(title: "本机状态: 正在读取...", action: nil, keyEquivalent: "")
    private let edge = NSMenuItem(title: "边缘通道: 已停止", action: nil, keyEquivalent: "")
    private let centralCommands = NSMenuItem(title: "中台指令: 等待边缘通道", action: nil, keyEquivalent: "")
    private var snapshot = RuntimeSnapshot(states: [], events: [])
    private let control: String
    private var detailWindow: NSWindow?
    private var floatingPanel: FloatingStatusPanel?
    private var dashboard: FloatingDashboardView?
    private var timer: Timer?
    private var refreshing = false
    private var actionInFlight = false
    private var actionNotice = ""
    private var refreshError = ""

    override init() {
        let app = URL(fileURLWithPath: Bundle.main.bundlePath)
        let resourceRoot = Bundle.main.url(forResource: "wecom-gui-root", withExtension: "txt")
            .flatMap { try? String(contentsOf: $0, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines) }
        root = ProcessInfo.processInfo.environment["WECOM_GUI_ROOT"] ?? resourceRoot ?? app.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path
        control = ProcessInfo.processInfo.environment["WECOM_CONTROL_PATH"] ?? "\(root)/scripts/wecom-control"
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        item.button?.image = indicator(.systemGray)
        makeFloatingDashboard()
        menu.addItem(summary)
        menu.addItem(edge)
        menu.addItem(centralCommands)
        menu.addItem(.separator())
        add("启动边缘通道", #selector(startEdge)); add("停止边缘通道", #selector(stopEdge)); add("重启边缘通道", #selector(restartEdge))
        add("启动企微", #selector(startWeCom)); add("停止企微", #selector(stopWeCom))
        add("检查系统权限", #selector(checkPermissions))
        menu.addItem(.separator())
        add("显示/隐藏状态浮窗", #selector(toggleDashboard)); add("查看本机流转详情", #selector(showDetails)); add("打开运行日志", #selector(openLogs)); add("打开中台工作台", #selector(openWorkbench))
        menu.addItem(.separator()); add("退出 UDA WeCom Agent", #selector(quit))
        item.menu = menu
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }

    private func makeFloatingDashboard() {
        let size = NSSize(width: 360, height: 520)
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let origin = NSPoint(x: screen.maxX - size.width - 24, y: screen.maxY - size.height - 56)
        let panel = FloatingStatusPanel(
            contentRect: NSRect(origin: origin, size: size),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        let view = FloatingDashboardView(frame: NSRect(origin: .zero, size: size))
        view.onDetails = { [weak self] in self?.showDetails() }
        view.onLogs = { [weak self] in self?.openLogs() }
        view.onWorkbench = { [weak self] in self?.openWorkbench() }
        view.onStartWeCom = { [weak self] in self?.controlAction("start-wecom") }
        view.onStopWeCom = { [weak self] in self?.controlAction("stop-wecom") }
        view.onStartEdge = { [weak self] in self?.controlAction("start-edge") }
        view.onStopEdge = { [weak self] in self?.controlAction("stop-edge") }
        panel.contentView = view
        floatingPanel = panel
        dashboard = view
        panel.orderFrontRegardless()
    }

    private func add(_ title: String, _ action: Selector) { menu.addItem(NSMenuItem(title: title, action: action, keyEquivalent: "")) }
    private func indicator(_ color: NSColor) -> NSImage? {
        NSImage(systemSymbolName: "circle.fill", accessibilityDescription: "UDA WeCom Agent")?.withSymbolConfiguration(.init(paletteColors: [color]))
    }

    private func invoke(_ executable: String, _ args: [String], completion: @escaping (String, Bool) -> Void) {
        let directory = root
        DispatchQueue.global(qos: .utility).async {
            let task = Process(); task.currentDirectoryURL = URL(fileURLWithPath: directory)
            task.executableURL = URL(fileURLWithPath: executable)
            task.arguments = args
            let pipe = Pipe(); task.standardOutput = pipe; task.standardError = pipe
            do {
                try task.run()
                // Drain while the child runs; waiting for exit first can fill the pipe.
                let result = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                task.waitUntilExit()
                let success = task.terminationStatus == 0
                DispatchQueue.main.async { completion(result, success) }
            } catch {
                DispatchQueue.main.async { completion("\(error)", false) }
            }
        }
    }

    private func refresh() {
        guard !refreshing else { return }
        refreshing = true
        invoke(control, ["runtime-status"]) { [weak self] text, success in
            guard let self else { return }
            self.refreshing = false
            if success, let data = text.data(using: .utf8), let snapshot = try? JSONDecoder().decode(RuntimeSnapshot.self, from: data) {
                self.snapshot = snapshot
                self.refreshError = ""
            } else {
                self.refreshError = "状态读取失败，请查看运行日志"
            }
            self.render()
        }
    }

    private func render() {
        let access = DesktopAccess.current()
        let edgeState = snapshot.states.first { $0.process == "edge_channel" }
        edge.title = line("边缘通道", edgeState)
        centralCommands.title = commandLine(edgeState)
        let statuses = [edgeState?.status].compactMap { $0 }
        let color: NSColor = statuses.contains("failed") ? .systemRed : (statuses.contains("waiting") || statuses.contains("retrying")) ? .systemYellow : (statuses.contains("running") || statuses.contains("idle")) ? .systemGreen : .systemGray
        item.button?.image = indicator(color)
        summary.title = color == .systemRed ? "本机状态: 需要处理" : color == .systemYellow ? "本机状态: 等待确认或重试" : color == .systemGreen ? "本机状态: 正常" : "本机状态: 服务已停止"
        dashboard?.update(snapshot)
        let issue = access.issue ?? (refreshError.isEmpty ? nil : refreshError)
        dashboard?.controlNotice = issue ?? actionNotice
        if let issue {
            item.button?.image = indicator(.systemRed)
            summary.title = "本机状态: \(issue)"
        }
        writeDesktopHealth(access)
    }

    private func writeDesktopHealth(_ access: DesktopAccess) {
        let health: [String: Any] = ["pid": ProcessInfo.processInfo.processIdentifier,
            "accessibility": access.accessibility, "screen_capture": access.screenCapture,
            "state_read_ok": refreshError.isEmpty, "action_in_flight": actionInFlight,
            "updated_at": Date().timeIntervalSince1970]
        let directory = URL(fileURLWithPath: "\(root)/.codex-run")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: health, options: [.sortedKeys])
            try data.write(to: directory.appendingPathComponent("desktop-health.json"), options: .atomic)
        } catch { NSLog("Unable to write desktop health: %@", error.localizedDescription) }
    }

    private func line(_ title: String, _ value: RuntimeProcess?) -> String {
        guard let value else { return "\(title): 已停止" }
        let detail = value.conversation_label.isEmpty ? "" : " · \(value.conversation_label)"
        let error = value.error_code.isEmpty ? "" : " · \(value.error_code)"
        return "\(title): \(label(value.status)) · \(value.phase)\(detail)\(error)"
    }

    private func label(_ status: String) -> String {
        ["idle": "正常", "running": "运行中", "waiting": "等待确认", "retrying": "重试中", "failed": "失败", "stopped": "已停止"][status] ?? status
    }

    private func commandLine(_ edgeState: RuntimeProcess?) -> String {
        guard let edgeState else { return "中台指令: 等待边缘通道" }
        if edgeState.status == "failed" { return "中台指令: 连接异常" }
        if edgeState.phase == "waiting_for_command" { return "中台指令: 长轮询等待中" }
        return "中台指令: 经边缘通道流转"
    }

    private func supervise(_ action: String, _ service: String, completion: (() -> Void)? = nil) {
        controlAction("\(action)-\(service)", completion: completion)
    }
    private func controlAction(_ action: String, completion: (() -> Void)? = nil) {
        guard !actionInFlight else { return }
        if ["start-edge", "restart-edge"].contains(action), !ensureEdgePermissions() { return }
        actionInFlight = true
        actionNotice = "正在执行操作..."
        render()
        invoke(control, [action]) { [weak self] text, success in
            guard let self else { return }
            self.actionInFlight = false
            self.actionNotice = success ? "操作已执行" : "操作失败，请查看错误提示"
            self.refresh()
            if !success { self.showCommandError(text) }
            completion?()
        }
    }
    private func ensureEdgePermissions() -> Bool {
        let access = DesktopAccess.current()
        guard let issue = access.issue else { return true }
        render()
        let alert = NSAlert()
        alert.messageText = issue
        alert.informativeText = !access.accessibility
            ? "请在系统设置的隐私与安全性 > 辅助功能中授权 UDA WeCom Agent。更新后如果已经开启，请移除旧条目，再添加 ~/Applications/UDA WeCom Agent.app。授权完成后重新点击启动边缘。"
            : "请在系统设置的隐私与安全性 > 屏幕与系统音频录制中允许 UDA WeCom Agent，用于读取消息气泡方向和图片。授权后重新打开客户端，再点击启动边缘。"
        alert.addButton(withTitle: "打开系统设置")
        alert.addButton(withTitle: "稍后")
        if alert.runModal() == .alertFirstButtonReturn {
            if !access.accessibility {
                let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
                _ = AXIsProcessTrustedWithOptions(options)
                NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
            } else {
                _ = CGRequestScreenCaptureAccess()
                NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
            }
        }
        return false
    }
    @objc private func checkPermissions() { _ = ensureEdgePermissions() }
    private func showCommandError(_ text: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "操作未完成"
        alert.informativeText = text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "统一控制脚本执行失败。" : text.trimmingCharacters(in: .whitespacesAndNewlines)
        alert.addButton(withTitle: "知道了")
        alert.runModal()
    }
    @objc private func startEdge() { supervise("start", "edge") }
    @objc private func stopEdge() { supervise("stop", "edge") }
    @objc private func restartEdge() { supervise("restart", "edge") }
    @objc private func startWeCom() { controlAction("start-wecom") }
    @objc private func stopWeCom() { controlAction("stop-wecom") }
    @objc private func toggleDashboard() {
        guard let floatingPanel else { return }
        if floatingPanel.isVisible { floatingPanel.orderOut(nil) }
        else { floatingPanel.orderFrontRegardless() }
    }
    @objc private func openLogs() { NSWorkspace.shared.open(URL(fileURLWithPath: "\(root)/.codex-run")) }
    @objc private func openWorkbench() { NSWorkspace.shared.open(URL(string: ProcessInfo.processInfo.environment["WECOM_WORKBENCH_URL"] ?? "https://knowledge-cs.uda.cn/operations/wecom-message-workbench")!) }
    @objc private func quit() { NSApp.terminate(nil) }

    @objc private func showDetails() {
        let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 760, height: 520)); text.isEditable = false
        text.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        let formatter = ISO8601DateFormatter()
        text.string = snapshot.events.prefix(20).map { item in
            let details = item.metrics.map { "\($0.key)=\($0.value)" }.sorted().joined(separator: ", ")
            let conversation = item.conversation_label.isEmpty ? item.conversation_key : item.conversation_label
            return "\(formatter.string(from: Date(timeIntervalSince1970: item.occurred_at)))  \(item.process)  \(label(item.status))  \(item.phase)\n会话: \(conversation)  方向: \(item.direction.isEmpty ? "-" : item.direction)  原因: \(item.rationale)\(details.isEmpty ? "" : "  参数: \(details)")\(item.error_code.isEmpty ? "" : "  错误: \(item.error_code)")\n"
        }.joined(separator: "\n")
        if text.string.isEmpty { text.string = "暂无本机活动轨迹。" }
        let scroll = NSScrollView(frame: text.bounds); scroll.documentView = text; scroll.hasVerticalScroller = true; scroll.autoresizingMask = [.width, .height]
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 520), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "UDA WeCom Agent - 本机流转轨迹"; window.contentView = scroll; detailWindow = window; window.makeKeyAndOrderFront(nil)
    }
}

private let app = NSApplication.shared
private let delegate = AgentApp()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
