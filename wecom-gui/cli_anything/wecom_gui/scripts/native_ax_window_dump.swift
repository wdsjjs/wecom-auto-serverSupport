import Foundation
import ApplicationServices
import AppKit

let defaultBundleID = "com.tencent.WeWorkMac"

func envString(_ name: String, _ defaultValue: String) -> String {
    let value = ProcessInfo.processInfo.environment[name]?.trimmingCharacters(in: .whitespacesAndNewlines)
    return value?.isEmpty == false ? value! : defaultValue
}

func envInt(_ name: String, _ defaultValue: Int) -> Int {
    guard let raw = ProcessInfo.processInfo.environment[name],
          let value = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines)) else {
        return defaultValue
    }
    return value
}

func envDouble(_ name: String, _ defaultValue: Double) -> Double {
    guard let raw = ProcessInfo.processInfo.environment[name],
          let value = Double(raw.trimmingCharacters(in: .whitespacesAndNewlines)) else {
        return defaultValue
    }
    return value
}

func envBool(_ name: String, _ defaultValue: Bool) -> Bool {
    guard let raw = ProcessInfo.processInfo.environment[name]?.lowercased().trimmingCharacters(in: .whitespacesAndNewlines) else {
        return defaultValue
    }
    if ["1", "true", "yes", "y", "on"].contains(raw) {
        return true
    }
    if ["0", "false", "no", "n", "off"].contains(raw) {
        return false
    }
    return defaultValue
}

func isoNow() -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: Date())
}

func durationMs(since start: Date) -> Double {
    return (Date().timeIntervalSince(start) * 1000.0).rounded() / 1.0
}

func stderr(_ text: String) {
    if let data = "\(text)\n".data(using: .utf8) {
        FileHandle.standardError.write(data)
    }
}

func copyAttribute(_ element: AXUIElement, _ attr: String) -> CFTypeRef? {
    var value: CFTypeRef?
    let result = AXUIElementCopyAttributeValue(element, attr as CFString, &value)
    guard result == .success else {
        return nil
    }
    return value
}

func attributeNames(_ element: AXUIElement) -> [String] {
    var value: CFArray?
    let result = AXUIElementCopyAttributeNames(element, &value)
    guard result == .success, let names = value as? [String] else {
        return []
    }
    return names.sorted()
}

func stringAttribute(_ element: AXUIElement, _ attr: String) -> String? {
    guard let value = copyAttribute(element, attr) else {
        return nil
    }
    if let string = value as? String, !string.isEmpty {
        return string
    }
    if let attributed = value as? NSAttributedString, !attributed.string.isEmpty {
        return attributed.string
    }
    return nil
}

func boolAttribute(_ element: AXUIElement, _ attr: String) -> Bool? {
    guard let value = copyAttribute(element, attr) else {
        return nil
    }
    if CFGetTypeID(value) == CFBooleanGetTypeID() {
        return (value as! Bool)
    }
    if let number = value as? NSNumber {
        return number.boolValue
    }
    return nil
}

func pointAttribute(_ element: AXUIElement, _ attr: String) -> CGPoint? {
    guard let value = copyAttribute(element, attr),
          CFGetTypeID(value) == AXValueGetTypeID(),
          AXValueGetType(value as! AXValue) == .cgPoint else {
        return nil
    }
    var point = CGPoint.zero
    return AXValueGetValue(value as! AXValue, .cgPoint, &point) ? point : nil
}

func sizeAttribute(_ element: AXUIElement, _ attr: String) -> CGSize? {
    guard let value = copyAttribute(element, attr),
          CFGetTypeID(value) == AXValueGetTypeID(),
          AXValueGetType(value as! AXValue) == .cgSize else {
        return nil
    }
    var size = CGSize.zero
    return AXValueGetValue(value as! AXValue, .cgSize, &size) ? size : nil
}

func rectForElement(_ element: AXUIElement) -> [String: Double]? {
    guard let point = pointAttribute(element, kAXPositionAttribute as String),
          let size = sizeAttribute(element, kAXSizeAttribute as String) else {
        return nil
    }
    return [
        "x": Double(point.x),
        "y": Double(point.y),
        "width": Double(size.width),
        "height": Double(size.height)
    ]
}

func childrenOf(_ element: AXUIElement) -> [AXUIElement] {
    guard let value = copyAttribute(element, kAXChildrenAttribute as String) else {
        return []
    }
    return value as? [AXUIElement] ?? []
}

func serializeAXValue(_ value: AXValue) -> Any {
    switch AXValueGetType(value) {
    case .cgPoint:
        var point = CGPoint.zero
        if AXValueGetValue(value, .cgPoint, &point) {
            return ["x": Double(point.x), "y": Double(point.y)]
        }
    case .cgSize:
        var size = CGSize.zero
        if AXValueGetValue(value, .cgSize, &size) {
            return ["width": Double(size.width), "height": Double(size.height)]
        }
    case .cgRect:
        var rect = CGRect.zero
        if AXValueGetValue(value, .cgRect, &rect) {
            return [
                "x": Double(rect.origin.x),
                "y": Double(rect.origin.y),
                "width": Double(rect.size.width),
                "height": Double(rect.size.height)
            ]
        }
    case .cfRange:
        var range = CFRange()
        if AXValueGetValue(value, .cfRange, &range) {
            return ["location": range.location, "length": range.length]
        }
    default:
        break
    }
    return String(describing: value)
}

func serializeValue(_ raw: CFTypeRef, nestedArrayLimit: Int = 12) -> Any {
    if CFGetTypeID(raw) == AXValueGetTypeID() {
        return serializeAXValue(raw as! AXValue)
    }
    if CFGetTypeID(raw) == AXUIElementGetTypeID() {
        let element = raw as! AXUIElement
        var payload: [String: Any] = ["ax_element": true]
        if let role = stringAttribute(element, kAXRoleAttribute as String) {
            payload["role"] = role
        }
        if let title = stringAttribute(element, kAXTitleAttribute as String) {
            payload["title"] = title
        }
        if let rect = rectForElement(element) {
            payload["rect"] = rect
        }
        return payload
    }
    if let string = raw as? String {
        return string
    }
    if let attributed = raw as? NSAttributedString {
        return attributed.string
    }
    if CFGetTypeID(raw) == CFBooleanGetTypeID() {
        return raw as! Bool
    }
    if let number = raw as? NSNumber {
        return number
    }
    if let array = raw as? [Any] {
        var items: [Any] = []
        for item in array.prefix(nestedArrayLimit) {
            let cfItem = item as CFTypeRef
            items.append(serializeValue(cfItem, nestedArrayLimit: 3))
        }
        var payload: [String: Any] = ["count": array.count, "items": items]
        if array.count > nestedArrayLimit {
            payload["truncated"] = true
        }
        return payload
    }
    return String(describing: raw)
}

func appForBundleID(_ bundleID: String) -> NSRunningApplication? {
    return NSWorkspace.shared.runningApplications.first { app in
        app.bundleIdentifier == bundleID && !app.isTerminated
    }
}

func preferredWindow(_ appElement: AXUIElement) -> AXUIElement? {
    let preferredAttrs = [
        kAXFocusedWindowAttribute as String,
        kAXMainWindowAttribute as String
    ]
    for attr in preferredAttrs {
        if let value = copyAttribute(appElement, attr),
           CFGetTypeID(value) == AXUIElementGetTypeID() {
            return (value as! AXUIElement)
        }
    }
    if let value = copyAttribute(appElement, kAXWindowsAttribute as String),
       let windows = value as? [AXUIElement],
       let first = windows.first {
        return first
    }
    return nil
}

let canonicalAttributes = [
    kAXRoleAttribute as String,
    kAXSubroleAttribute as String,
    kAXRoleDescriptionAttribute as String,
    kAXTitleAttribute as String,
    kAXValueAttribute as String,
    kAXDescriptionAttribute as String,
    kAXHelpAttribute as String,
    kAXIdentifierAttribute as String,
    kAXPlaceholderValueAttribute as String,
    kAXPositionAttribute as String,
    kAXSizeAttribute as String,
    kAXSelectedAttribute as String,
    kAXFocusedAttribute as String,
    kAXEnabledAttribute as String,
    kAXExpandedAttribute as String,
    kAXMainAttribute as String,
    kAXMinimizedAttribute as String
]

let skippedFullAttributes = Set([
    kAXChildrenAttribute as String,
    kAXVisibleChildrenAttribute as String,
    kAXSelectedChildrenAttribute as String,
    kAXParentAttribute as String,
    kAXWindowAttribute as String,
    kAXTopLevelUIElementAttribute as String
])

func elementPayload(_ element: AXUIElement, index: Int, parentIndex: Int?, depth: Int, path: String, childCount: Int, includeAllAttributes: Bool) -> [String: Any] {
    var payload: [String: Any] = [
        "index": index,
        "depth": depth,
        "path": path,
        "child_count": childCount
    ]
    if let parentIndex {
        payload["parent_index"] = parentIndex
    }
    if let rect = rectForElement(element) {
        payload["rect"] = rect
    }

    var attributes: [String: Any] = [:]
    let names = includeAllAttributes ? attributeNames(element) : canonicalAttributes
    for name in names {
        if skippedFullAttributes.contains(name) {
            continue
        }
        if let value = copyAttribute(element, name) {
            attributes[name] = serializeValue(value)
        }
    }
    if !attributes.isEmpty {
        payload["attributes"] = attributes
    }

    var texts: [String] = []
    for name in [kAXTitleAttribute as String, kAXValueAttribute as String, kAXDescriptionAttribute as String, kAXHelpAttribute as String, kAXPlaceholderValueAttribute as String] {
        if let value = attributes[name] as? String {
            let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty && !texts.contains(text) {
                texts.append(text)
            }
        }
    }
    if !texts.isEmpty {
        payload["texts"] = texts
    }
    return payload
}

func dumpWindow(_ window: AXUIElement, label: String, maxDepth: Int, maxElements: Int, includeAllAttributes: Bool) -> [String: Any] {
    let started = Date()
    var elements: [[String: Any]] = []
    var truncated = false

    func walk(_ element: AXUIElement, parentIndex: Int?, depth: Int, path: String) {
        if elements.count >= maxElements {
            truncated = true
            return
        }
        let children = depth < maxDepth ? childrenOf(element) : []
        if depth >= maxDepth && !childrenOf(element).isEmpty {
            truncated = true
        }
        let currentIndex = elements.count
        let payload = elementPayload(
            element,
            index: currentIndex,
            parentIndex: parentIndex,
            depth: depth,
            path: path,
            childCount: children.count,
            includeAllAttributes: includeAllAttributes
        )
        elements.append(payload)
        if depth >= maxDepth {
            return
        }
        for (childOffset, child) in children.enumerated() {
            walk(child, parentIndex: currentIndex, depth: depth + 1, path: "\(path).\(childOffset)")
            if elements.count >= maxElements {
                truncated = true
                break
            }
        }
    }

    walk(window, parentIndex: nil, depth: 0, path: "0")

    let textElements = elements.filter { payload in
        guard let texts = payload["texts"] as? [String] else {
            return false
        }
        return !texts.isEmpty
    }
    let mediaLikeCount = elements.filter { payload in
        guard let attrs = payload["attributes"] as? [String: Any] else {
            return false
        }
        let role = attrs[kAXRoleAttribute as String] as? String ?? ""
        let desc = attrs[kAXDescriptionAttribute as String] as? String ?? ""
        return role.contains("Image") || desc.contains("图片") || desc.lowercased().contains("image")
    }.count

    return [
        "label": label,
        "started_at": isoNow(),
        "duration_ms": durationMs(since: started),
        "element_count": elements.count,
        "text_element_count": textElements.count,
        "media_like_element_count": mediaLikeCount,
        "truncated": truncated,
        "elements": elements
    ]
}

func scrollAt(x: Int, y: Int, delta: Int) -> [String: Any] {
    let started = Date()
    var payload: [String: Any] = [
        "x": x,
        "y": y,
        "delta": delta,
        "started_at": isoNow()
    ]
    guard let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left) else {
        payload["ok"] = false
        payload["error"] = "failed_to_create_mouse_move_event"
        payload["duration_ms"] = durationMs(since: started)
        return payload
    }
    move.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.08)
    guard let scroll = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: Int32(delta), wheel2: 0, wheel3: 0) else {
        payload["ok"] = false
        payload["error"] = "failed_to_create_scroll_event"
        payload["duration_ms"] = durationMs(since: started)
        return payload
    }
    scroll.post(tap: .cghidEventTap)
    payload["ok"] = true
    payload["duration_ms"] = durationMs(since: started)
    return payload
}

func writeJSON(_ payload: [String: Any], to outputPath: String?) {
    let options: JSONSerialization.WritingOptions = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    guard JSONSerialization.isValidJSONObject(payload),
          let data = try? JSONSerialization.data(withJSONObject: payload, options: options) else {
        stderr("ERROR: failed to serialize JSON payload")
        exit(2)
    }
    if let outputPath, !outputPath.isEmpty {
        do {
            try data.write(to: URL(fileURLWithPath: outputPath))
            print(outputPath)
        } catch {
            stderr("ERROR: failed to write \(outputPath): \(error)")
            exit(2)
        }
    } else if let text = String(data: data, encoding: .utf8) {
        print(text)
    }
}

let totalStarted = Date()
let outputPath = CommandLine.arguments.dropFirst().first
let bundleID = envString("WECOM_NATIVE_AX_BUNDLE_ID", envString("WECOM_GUI_BUNDLE_ID", defaultBundleID))
let maxDepth = envInt("WECOM_NATIVE_AX_MAX_DEPTH", 32)
let maxElements = envInt("WECOM_NATIVE_AX_MAX_ELEMENTS", 30000)
let includeAllAttributes = envBool("WECOM_NATIVE_AX_INCLUDE_ALL_ATTRS", false)
let activateApp = envBool("WECOM_NATIVE_AX_ACTIVATE", true)
let restoreScroll = envBool("WECOM_NATIVE_AX_RESTORE_SCROLL", true)
let scrollDelta = envInt("WECOM_NATIVE_AX_SCROLL_DELTA", -6)
let settleMs = envInt("WECOM_NATIVE_AX_SETTLE_MS", 500)
let scrollXRatio = envDouble("WECOM_NATIVE_AX_SCROLL_X_RATIO", 0.62)
let scrollYRatio = envDouble("WECOM_NATIVE_AX_SCROLL_Y_RATIO", 0.56)

var payload: [String: Any] = [
    "created_at": isoNow(),
    "purpose": "native_accessibility_api_dump_current_wecom_window_before_and_after_one_scroll",
    "bundle_id": bundleID,
    "accessibility_trusted": AXIsProcessTrusted(),
    "config": [
        "max_depth": maxDepth,
        "max_elements": maxElements,
        "include_all_attributes": includeAllAttributes,
        "activate_app": activateApp,
        "restore_scroll": restoreScroll,
        "scroll_delta": scrollDelta,
        "settle_ms": settleMs,
        "scroll_x_ratio": scrollXRatio,
        "scroll_y_ratio": scrollYRatio
    ]
]

guard let runningApp = appForBundleID(bundleID) else {
    payload["ok"] = false
    payload["error"] = "wecom_process_not_found"
    payload["duration_ms"] = durationMs(since: totalStarted)
    writeJSON(payload, to: outputPath)
    exit(1)
}

payload["process"] = [
    "pid": runningApp.processIdentifier,
    "localized_name": runningApp.localizedName ?? "",
    "bundle_identifier": runningApp.bundleIdentifier ?? "",
    "activation_policy": runningApp.activationPolicy.rawValue
]

if activateApp {
    runningApp.activate(options: [])
    Thread.sleep(forTimeInterval: 0.35)
}

let axApp = AXUIElementCreateApplication(runningApp.processIdentifier)
guard let window = preferredWindow(axApp) else {
    payload["ok"] = false
    payload["error"] = "wecom_window_not_found"
    payload["duration_ms"] = durationMs(since: totalStarted)
    writeJSON(payload, to: outputPath)
    exit(1)
}

let windowRect = rectForElement(window) ?? [:]
payload["window"] = [
    "title": stringAttribute(window, kAXTitleAttribute as String) ?? "",
    "role": stringAttribute(window, kAXRoleAttribute as String) ?? "",
    "subrole": stringAttribute(window, kAXSubroleAttribute as String) ?? "",
    "rect": windowRect
]

let winX = windowRect["x"] ?? 0
let winY = windowRect["y"] ?? 0
let winWidth = windowRect["width"] ?? 0
let winHeight = windowRect["height"] ?? 0
let scrollX = Int(winX + winWidth * scrollXRatio)
let scrollY = Int(winY + winHeight * scrollYRatio)
payload["scroll_point"] = ["x": scrollX, "y": scrollY]

payload["before"] = dumpWindow(
    window,
    label: "before_scroll",
    maxDepth: maxDepth,
    maxElements: maxElements,
    includeAllAttributes: includeAllAttributes
)

payload["scroll_down"] = scrollAt(x: scrollX, y: scrollY, delta: scrollDelta)
Thread.sleep(forTimeInterval: Double(max(0, settleMs)) / 1000.0)

payload["after_scroll"] = dumpWindow(
    window,
    label: "after_scroll",
    maxDepth: maxDepth,
    maxElements: maxElements,
    includeAllAttributes: includeAllAttributes
)

if restoreScroll {
    payload["scroll_restore"] = scrollAt(x: scrollX, y: scrollY, delta: -scrollDelta)
}

payload["ok"] = true
payload["duration_ms"] = durationMs(since: totalStarted)
writeJSON(payload, to: outputPath)
