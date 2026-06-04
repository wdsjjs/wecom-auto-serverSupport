import Foundation
import ApplicationServices
import AppKit

func jsonLine(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object, options: []),
       let string = String(data: data, encoding: .utf8) {
        print(string)
    }
}

func stringAttr(_ element: AXUIElement, _ attr: CFString) -> String? {
    var value: CFTypeRef?
    if AXUIElementCopyAttributeValue(element, attr, &value) != .success {
        return nil
    }
    if let string = value as? String, !string.isEmpty {
        return string
    }
    if let attrString = value as? NSAttributedString, !attrString.string.isEmpty {
        return attrString.string
    }
    return nil
}

func role(_ element: AXUIElement) -> String {
    return stringAttr(element, kAXRoleAttribute as CFString) ?? ""
}

func subrole(_ element: AXUIElement) -> String {
    return stringAttr(element, kAXSubroleAttribute as CFString) ?? ""
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    var value: CFTypeRef?
    if AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) != .success {
        return []
    }
    return value as? [AXUIElement] ?? []
}

func pointAttr(_ element: AXUIElement, _ attr: CFString) -> CGPoint? {
    var value: CFTypeRef?
    if AXUIElementCopyAttributeValue(element, attr, &value) != .success {
        return nil
    }
    guard let axValue = value, CFGetTypeID(axValue) == AXValueGetTypeID() else {
        return nil
    }
    var point = CGPoint.zero
    if AXValueGetValue(axValue as! AXValue, .cgPoint, &point) {
        return point
    }
    return nil
}

func sizeAttr(_ element: AXUIElement) -> CGSize? {
    var value: CFTypeRef?
    if AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &value) != .success {
        return nil
    }
    guard let axValue = value, CFGetTypeID(axValue) == AXValueGetTypeID() else {
        return nil
    }
    var size = CGSize.zero
    if AXValueGetValue(axValue as! AXValue, .cgSize, &size) {
        return size
    }
    return nil
}

func rectPayload(_ element: AXUIElement) -> [String: Double]? {
    guard let pos = pointAttr(element, kAXPositionAttribute as CFString),
          let size = sizeAttr(element) else {
        return nil
    }
    return [
        "x": Double(pos.x),
        "y": Double(pos.y),
        "width": Double(size.width),
        "height": Double(size.height)
    ]
}

func textValues(_ element: AXUIElement) -> [String] {
    var parts: [String] = []
    for attr in [kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute] {
        if let text = stringAttr(element, attr as CFString), !parts.contains(text) {
            parts.append(text)
        }
    }
    return parts
}

func collectText(_ element: AXUIElement, maxDepth: Int = 8) -> [String] {
    var out: [String] = []
    func walk(_ element: AXUIElement, _ depth: Int) {
        if depth > maxDepth {
            return
        }
        for text in textValues(element) {
            if !out.contains(text) {
                out.append(text)
            }
        }
        for child in children(element) {
            walk(child, depth + 1)
        }
    }
    walk(element, 0)
    return out
}

func windowLooksLikeImagePreview(_ window: AXUIElement) -> Bool {
    var values = collectText(window, maxDepth: 6).map { $0.lowercased() }
    if let title = stringAttr(window, kAXTitleAttribute as CFString) {
        values.append(title.lowercased())
    }
    let hasImageTitle = values.contains { $0 == "图片" || $0 == "image" || $0.contains("图片") }
    let hasPreviewControl = values.contains { value in
        value.contains("上一张")
            || value.contains("下一张")
            || value.contains("放大")
            || value.contains("缩小")
            || value.contains("保存到本地")
            || value.contains("提取文字")
            || value.contains("翻译")
    }
    return hasImageTitle && hasPreviewControl
}

func previewContentFallbackRect(_ window: AXUIElement) -> [String: Any]? {
    guard let rect = rectPayload(window) else {
        return nil
    }
    let width = rect["width", default: 0]
    let height = rect["height", default: 0]
    let screenRect = NSScreen.main?.frame ?? .zero
    if width <= 160 || height <= 160 {
        return nil
    }
    if width > Double(screenRect.width) * 0.98 || height > Double(screenRect.height) * 0.98 {
        return nil
    }
    let sideInset = max(8.0, width * 0.06)
    let topInset = 28.0
    let bottomInset = max(72.0, min(110.0, height * 0.18))
    return [
        "role": "AXWindowContentFallback",
        "subrole": "",
        "texts": ["图片"],
        "x": rect["x", default: 0] + sideInset,
        "y": rect["y", default: 0] + topInset,
        "width": max(1.0, width - sideInset * 2),
        "height": max(1.0, height - topInset - bottomInset)
    ]
}

func windowLooksLikeDetachedPreview(_ window: AXUIElement) -> Bool {
    guard let rect = rectPayload(window) else {
        return false
    }
    let width = rect["width", default: 0]
    let height = rect["height", default: 0]
    if width < 320 || width > 900 || height < 240 || height > 900 {
        return false
    }
    if height < 80 || width / max(height, 1) > 4.0 {
        return false
    }
    let title = (stringAttr(window, kAXTitleAttribute as CFString) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    return title.isEmpty || title == "图片" || title.lowercased() == "image"
}

func collectRows(_ element: AXUIElement, rows: inout [AXUIElement], maxDepth: Int = 12, depth: Int = 0) {
    if depth > maxDepth {
        return
    }
    if role(element) == "AXRow" {
        rows.append(element)
    }
    for child in children(element) {
        collectRows(child, rows: &rows, maxDepth: maxDepth, depth: depth + 1)
    }
}

func collectTables(_ element: AXUIElement, tables: inout [AXUIElement], maxDepth: Int = 12, depth: Int = 0) {
    if depth > maxDepth {
        return
    }
    if role(element) == "AXTable" {
        tables.append(element)
    }
    for child in children(element) {
        collectTables(child, tables: &tables, maxDepth: maxDepth, depth: depth + 1)
    }
}

func appElement(bundleID: String) -> AXUIElement? {
    guard let app = NSWorkspace.shared.runningApplications.first(where: { $0.bundleIdentifier == bundleID }) else {
        return nil
    }
    return AXUIElementCreateApplication(app.processIdentifier)
}

func rowPayload(_ row: AXUIElement, index: Int) -> [String: Any] {
    let texts = collectText(row)
    let pos = pointAttr(row, kAXPositionAttribute as CFString) ?? .zero
    let size = sizeAttr(row) ?? .zero
    var selected = false
    var selectedValue: CFTypeRef?
    if AXUIElementCopyAttributeValue(row, kAXSelectedAttribute as CFString, &selectedValue) == .success {
        selected = (selectedValue as? Bool) ?? false
    }
    return [
        "index": index,
        "texts": texts,
        "x": Double(pos.x),
        "y": Double(pos.y),
        "width": Double(size.width),
        "height": Double(size.height),
        "selected": selected
    ]
}

func isLikelyTimeText(_ value: String) -> Bool {
    let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
    if text == "刚刚" || text.contains("分钟前") || text.contains("昨天") || text.contains("星期") {
        return true
    }
    if text.range(of: #"^\d{1,2}:\d{2}$"#, options: .regularExpression) != nil {
        return true
    }
    if text.range(of: #"^\d{1,2}/\d{1,2}$"#, options: .regularExpression) != nil {
        return true
    }
    return false
}

func rowTimeText(_ row: AXUIElement) -> String {
    let texts = collectText(row).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    for text in texts.reversed() where isLikelyTimeText(text) {
        return text
    }
    return ""
}

func rowHasUnreadMarker(_ row: AXUIElement) -> Bool {
    let texts = collectText(row)
    if texts.contains(where: { value in
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.range(of: #"^\d+$"#, options: .regularExpression) != nil
    }) {
        return true
    }
    var stack = children(row)
    while !stack.isEmpty {
        let item = stack.removeFirst()
        let itemRole = role(item)
        let values = textValues(item).map { $0.lowercased() }
        if itemRole == "AXImage" && values.contains(where: { value in
            value.contains("badge") || value.contains("unread") || value.contains("未读")
        }) {
            return true
        }
        stack.append(contentsOf: children(item))
    }
    return false
}

func looksLikeNavigationTable(_ table: AXUIElement) -> Bool {
    let texts = collectText(table, maxDepth: 5)
    return texts.contains("单聊") && (texts.contains("群聊") || texts.contains("@我") || texts.contains("未读"))
}

func navigationTables(root: AXUIElement, window: AXUIElement?) -> [AXUIElement] {
    var tables: [AXUIElement] = []
    if let window = window {
        collectTables(window, tables: &tables, maxDepth: 12)
    }
    if tables.isEmpty {
        collectTables(root, tables: &tables, maxDepth: 12)
    }
    return tables.filter { looksLikeNavigationTable($0) }
}

func singleChatRow(root: AXUIElement, window: AXUIElement?) -> AXUIElement? {
    for table in navigationTables(root: root, window: window) {
        for row in children(table) where role(row) == "AXRow" {
            let texts = collectText(row).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            if texts.contains("单聊") {
                return row
            }
        }
    }
    var rows: [AXUIElement] = []
    if let window = window {
        collectRows(window, rows: &rows, maxDepth: 14)
    }
    if rows.isEmpty {
        collectRows(root, rows: &rows, maxDepth: 14)
    }
    return rows.first { row in
        collectText(row, maxDepth: 4).contains("单聊")
    }
}

func ensureSingleChat(root: AXUIElement, window: AXUIElement?) -> [String: Any] {
    guard let row = singleChatRow(root: root, window: window) else {
        return ["ok": false, "error": "single_chat_row_not_found"]
    }
    let payload = rowPayload(row, index: 1)
    let selected = payload["selected"] as? Bool ?? false
    if !selected {
        AXUIElementSetAttributeValue(row, kAXSelectedAttribute as CFString, boolValue(true))
        let press = AXUIElementPerformAction(row, kAXPressAction as CFString)
        if press != .success, let rect = rectPayload(row) {
            clickAt(
                x: rect["x", default: 0] + rect["width", default: 0] * 0.5,
                y: rect["y", default: 0] + rect["height", default: 0] * 0.5
            )
        }
        Thread.sleep(forTimeInterval: 0.18)
    }
    return [
        "ok": true,
        "selectedBefore": selected,
        "row": payload
    ]
}

func collectTextElements(_ element: AXUIElement, out: inout [[String: Any]], maxDepth: Int = 10, depth: Int = 0) {
    if depth > maxDepth {
        return
    }
    let elementRole = role(element)
    let values = textValues(element).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    if !values.isEmpty && (elementRole == "AXTextArea" || elementRole == "AXTextField" || elementRole == "AXStaticText") {
        let pos = pointAttr(element, kAXPositionAttribute as CFString) ?? .zero
        let size = sizeAttr(element) ?? .zero
        out.append([
            "role": elementRole,
            "texts": values,
            "x": Double(pos.x),
            "y": Double(pos.y),
            "width": Double(size.width),
            "height": Double(size.height)
        ])
    }
    for child in children(element) {
        collectTextElements(child, out: &out, maxDepth: maxDepth, depth: depth + 1)
    }
}

func collectMediaElements(_ element: AXUIElement, out: inout [[String: Any]], maxDepth: Int = 10, depth: Int = 0) {
    if depth > maxDepth {
        return
    }
    let elementRole = role(element)
    let elementSubrole = subrole(element)
    let values = textValues(element).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    if let rect = rectPayload(element) {
        let width = rect["width", default: 0]
        let height = rect["height", default: 0]
        let loweredValues = values.map { $0.lowercased() }
        let looksLikeNamedImage = values.contains { value in
            value.contains("图片") || value.lowercased().contains("image") || value.lowercased().contains("photo")
        }
        let looksLikeAnimatedMedia = loweredValues.contains { value in
            value.contains("动画表情")
                || value.contains("表情")
                || value.contains("贴纸")
                || value.contains("动图")
                || value.contains("sticker")
                || value.contains("emoji")
                || value.contains("gif")
        }
        let roleLooksLikeMedia = elementRole == "AXImage"
            || elementRole == "AXImageView"
            || elementSubrole.lowercased().contains("image")
            || looksLikeNamedImage
            || looksLikeAnimatedMedia
            || ((elementRole == "AXGroup" || elementRole == "AXButton") && values.isEmpty && width >= 48 && height >= 48)
        if roleLooksLikeMedia
            && width >= 32
            && height >= 32
            && width <= 640
            && height <= 640 {
            let mediaType = looksLikeAnimatedMedia ? "animated_sticker" : "image"
            out.append([
                "role": elementRole,
                "subrole": elementSubrole,
                "mediaType": mediaType,
                "skipCapture": looksLikeAnimatedMedia,
                "texts": values,
                "x": rect["x", default: 0],
                "y": rect["y", default: 0],
                "width": width,
                "height": height
            ])
        }
    }
    for child in children(element) {
        collectMediaElements(child, out: &out, maxDepth: maxDepth, depth: depth + 1)
    }
}

func chatPayload(_ row: AXUIElement, index: Int) -> [String: Any] {
    var payload = rowPayload(row, index: index)
    var textElements: [[String: Any]] = []
    collectTextElements(row, out: &textElements)
    var mediaElements: [[String: Any]] = []
    collectMediaElements(row, out: &mediaElements)
    let messageElements = textElements.filter { item in
        let values = item["texts"] as? [String] ?? []
        if values.isEmpty {
            return false
        }
        return values.contains { text in
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty || trimmed == "个人名片" || trimmed == "以上是打招呼内容" {
                return false
            }
            if trimmed.range(of: #"^\d{1,2}:\d{2}$"#, options: .regularExpression) != nil {
                return false
            }
            return true
        }
    }
    if let bubble = messageElements.max(by: {
        (($0["width"] as? Double) ?? 0) < (($1["width"] as? Double) ?? 0)
    }) {
        payload["bubbleX"] = bubble["x"]
        payload["bubbleY"] = bubble["y"]
        payload["bubbleWidth"] = bubble["width"]
        payload["bubbleHeight"] = bubble["height"]
        payload["bubbleTexts"] = bubble["texts"]
    }
    if !mediaElements.isEmpty {
        payload["mediaElements"] = mediaElements
        if let media = mediaElements.max(by: {
            ((($0["width"] as? Double) ?? 0) * (($0["height"] as? Double) ?? 0))
                < ((($1["width"] as? Double) ?? 0) * (($1["height"] as? Double) ?? 0))
        }) {
            payload["mediaX"] = media["x"]
            payload["mediaY"] = media["y"]
            payload["mediaWidth"] = media["width"]
            payload["mediaHeight"] = media["height"]
        }
    }
    return payload
}

func allWindows(_ app: AXUIElement) -> [AXUIElement] {
    var windows: [AXUIElement] = []
    var focused: CFTypeRef?
    if AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &focused) == .success,
       let window = focused {
        windows.append(window as! AXUIElement)
    }
    var value: CFTypeRef?
    if AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
       let axWindows = value as? [AXUIElement] {
        windows.append(contentsOf: axWindows)
    }
    return windows
}

func candidatePreviewWindow(_ windows: [AXUIElement]) -> AXUIElement? {
    var bestWindow: AXUIElement?
    var bestArea = 0.0
    let screenRect = NSScreen.main?.frame ?? .zero
    for window in windows {
        guard let rect = rectPayload(window) else {
            continue
        }
        let x = rect["x", default: 0]
        let y = rect["y", default: 0]
        let width = rect["width", default: 0]
        let height = rect["height", default: 0]
        let area = width * height
        if width < 180 || height < 160 || area < 30000 {
            continue
        }
        if width > Double(screenRect.width) * 0.98 || height > Double(screenRect.height) * 0.98 {
            continue
        }
        if x < -20 || y < -20 {
            continue
        }
        if area > bestArea {
            bestArea = area
            bestWindow = window
        }
    }
    return bestWindow
}

func doubleClickAt(x: Double, y: Double) {
    let source = CGEventSource(stateID: .hidSystemState)
    let point = CGPoint(x: x, y: y)
    CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
    for clickState in [1, 2] {
        let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)
        down?.setIntegerValueField(.mouseEventClickState, value: Int64(clickState))
        down?.post(tap: .cghidEventTap)
        let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)
        up?.setIntegerValueField(.mouseEventClickState, value: Int64(clickState))
        up?.post(tap: .cghidEventTap)
        Thread.sleep(forTimeInterval: 0.06)
    }
}

func clickAt(x: Double, y: Double) {
    let source = CGEventSource(stateID: .hidSystemState)
    let point = CGPoint(x: x, y: y)
    CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
    CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.1)
}

func collectButtons(_ element: AXUIElement, buttons: inout [AXUIElement], maxDepth: Int = 14, depth: Int = 0) {
    if depth > maxDepth {
        return
    }
    if role(element) == "AXButton" {
        buttons.append(element)
    }
    for child in children(element) {
        collectButtons(child, buttons: &buttons, maxDepth: maxDepth, depth: depth + 1)
    }
}

func activateRunningApp(bundleID: String) {
    if let app = NSWorkspace.shared.runningApplications.first(where: { $0.bundleIdentifier == bundleID }) {
        app.activate(options: [])
        Thread.sleep(forTimeInterval: 0.08)
    }
}

func buttonCenter(_ button: AXUIElement) -> CGPoint? {
    guard let pos = pointAttr(button, kAXPositionAttribute as CFString),
          let size = sizeAttr(button) else {
        return nil
    }
    return CGPoint(x: pos.x + size.width / 2, y: pos.y + size.height / 2)
}

func inputStillContains(_ input: AXUIElement, text: String) -> Bool {
    let current = stringAttr(input, kAXValueAttribute as CFString) ?? ""
    return current.trimmingCharacters(in: .whitespacesAndNewlines)
        == text.trimmingCharacters(in: .whitespacesAndNewlines)
}

func waitUntilInputChanges(_ input: AXUIElement, text: String, attempts: Int = 8, delay: TimeInterval = 0.15) -> Bool {
    for _ in 0..<attempts {
        if !inputStillContains(input, text: text) {
            return true
        }
        Thread.sleep(forTimeInterval: delay)
    }
    return !inputStillContains(input, text: text)
}

func pressReturnKey() {
    let source = CGEventSource(stateID: .hidSystemState)
    let down = CGEvent(keyboardEventSource: source, virtualKey: 36, keyDown: true)
    let up = CGEvent(keyboardEventSource: source, virtualKey: 36, keyDown: false)
    down?.post(tap: .cghidEventTap)
    up?.post(tap: .cghidEventTap)
}

func fallbackConfirmReturn(input: AXUIElement) -> [String: Any] {
    let confirm = AXUIElementPerformAction(input, kAXConfirmAction as CFString)
    Thread.sleep(forTimeInterval: 0.05)
    pressReturnKey()
    return ["method": "ax_confirm_return", "code": confirm.rawValue]
}

func findSendButton(root: AXUIElement, window: AXUIElement?, input: AXUIElement) -> AXUIElement? {
    var buttons: [AXUIElement] = []
    if let window = window {
        collectButtons(window, buttons: &buttons)
    }
    if buttons.isEmpty {
        collectButtons(root, buttons: &buttons)
    }
    let inputPos = pointAttr(input, kAXPositionAttribute as CFString) ?? .zero
    let inputSize = sizeAttr(input) ?? .zero
    let inputCenterY = inputPos.y + inputSize.height / 2
    let labelled = buttons.filter { button in
        textValues(button).contains { value in
            let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            return normalized == "发送" || normalized == "send" || normalized.contains("发送")
        }
    }
    if let button = labelled.min(by: { lhs, rhs in
        let lp = pointAttr(lhs, kAXPositionAttribute as CFString) ?? .zero
        let rp = pointAttr(rhs, kAXPositionAttribute as CFString) ?? .zero
        return abs(lp.y - inputCenterY) < abs(rp.y - inputCenterY)
    }) {
        return button
    }
    return buttons.filter { button in
        guard textValues(button).isEmpty,
              let pos = pointAttr(button, kAXPositionAttribute as CFString),
              let size = sizeAttr(button) else {
            return false
        }
        let centerX = pos.x + size.width / 2
        let centerY = pos.y + size.height / 2
        return centerX >= inputPos.x + max(120, inputSize.width - 180)
            && centerX <= inputPos.x + inputSize.width + 40
            && centerY >= inputPos.y - 20
            && centerY <= inputPos.y + inputSize.height + 70
            && size.width >= 36
            && size.width <= 120
            && size.height >= 24
            && size.height <= 80
    }.min(by: { lhs, rhs in
        let lp = pointAttr(lhs, kAXPositionAttribute as CFString) ?? .zero
        let rp = pointAttr(rhs, kAXPositionAttribute as CFString) ?? .zero
        return lp.x > rp.x
    })
}

func submitInput(root: AXUIElement, window: AXUIElement?, input: AXUIElement, text: String) -> [String: Any] {
    if let pos = pointAttr(input, kAXPositionAttribute as CFString),
       let size = sizeAttr(input) {
        clickAt(x: Double(pos.x + min(max(size.width - 16, 10), size.width / 2)), y: Double(pos.y + size.height / 2))
    }
    Thread.sleep(forTimeInterval: 0.12)
    if let button = findSendButton(root: root, window: window, input: input) {
        let press = AXUIElementPerformAction(button, kAXPressAction as CFString)
        var method = "ax_send_button"
        if press != .success, let center = buttonCenter(button) {
            clickAt(x: Double(center.x), y: Double(center.y))
            method = "cg_send_button"
        }
        if !waitUntilInputChanges(input, text: text) {
            let fallback = fallbackConfirmReturn(input: input)
            return [
                "method": "\(method)+\(fallback["method"] ?? "fallback")",
                "buttonCode": press.rawValue,
                "fallbackCode": fallback["code"] ?? 0
            ]
        }
        return ["method": method, "buttonCode": press.rawValue]
    }
    return fallbackConfirmReturn(input: input)
}

func previewPayload(root: AXUIElement) -> [String: Any] {
    let windows = allWindows(root)
    var bestWindow: AXUIElement?
    var bestMedia: [String: Any]?
    var bestArea = 0.0
    let minArea = 40000.0
    for window in windows {
        var media: [[String: Any]] = []
        collectMediaElements(window, out: &media, maxDepth: 12)
        for item in media {
            let width = item["width"] as? Double ?? 0
            let height = item["height"] as? Double ?? 0
            let area = width * height
            if area > bestArea {
                bestArea = area
                bestWindow = window
                bestMedia = item
            }
        }
    }
    for window in windows {
        if windowLooksLikeImagePreview(window), let fallback = previewContentFallbackRect(window) {
            return [
                "ok": true,
                "window": rectPayload(window) ?? [:],
                "image": fallback,
                "windowCount": windows.count,
                "fallback": "window-content"
            ]
        }
    }
    for window in windows {
        if windowLooksLikeDetachedPreview(window), let fallback = previewContentFallbackRect(window) {
            return [
                "ok": true,
                "window": rectPayload(window) ?? [:],
                "image": fallback,
                "windowCount": windows.count,
                "fallback": "window-content"
            ]
        }
    }
    if let window = candidatePreviewWindow(windows), let fallback = previewContentFallbackRect(window) {
        return [
            "ok": true,
            "window": rectPayload(window) ?? [:],
            "image": fallback,
            "windowCount": windows.count,
            "fallback": "candidate-window"
        ]
    }
    guard let window = bestWindow, let media = bestMedia, bestArea >= minArea else {
        return ["ok": false, "error": "preview_image_not_found", "windowCount": windows.count]
    }
    return [
        "ok": true,
        "window": rectPayload(window) ?? [:],
        "image": media,
        "windowCount": windows.count
    ]
}

func closePreview(root: AXUIElement) -> [String: Any] {
    let windows = allWindows(root)
    var bestWindow: AXUIElement?
    var bestArea = 0.0
    var previewWindow: AXUIElement?
    for window in windows {
        if previewWindow == nil && (windowLooksLikeImagePreview(window) || windowLooksLikeDetachedPreview(window)) {
            previewWindow = window
        }
        var media: [[String: Any]] = []
        collectMediaElements(window, out: &media, maxDepth: 12)
        let windowArea = media.map {
            (($0["width"] as? Double) ?? 0) * (($0["height"] as? Double) ?? 0)
        }.max() ?? 0
        if windowArea > bestArea {
            bestArea = windowArea
            bestWindow = window
        }
    }
    let targetWindow = previewWindow ?? candidatePreviewWindow(windows) ?? (bestArea >= 40000.0 ? bestWindow : nil)
    guard let targetWindow = targetWindow else {
        return ["ok": false, "error": "preview_window_not_found", "windowCount": windows.count]
    }
    for window in [targetWindow] {
        var closeValue: CFTypeRef?
        if AXUIElementCopyAttributeValue(window, kAXCloseButtonAttribute as CFString, &closeValue) == .success,
           let closeButton = closeValue {
            let result = AXUIElementPerformAction(closeButton as! AXUIElement, kAXPressAction as CFString)
            return ["ok": result == .success, "method": "AXCloseButton", "code": result.rawValue]
        }
        var stack = children(window)
        while !stack.isEmpty {
            let item = stack.removeFirst()
            let values = textValues(item).map { $0.lowercased() }
            let itemSubrole = subrole(item)
            if itemSubrole == "AXCloseButton"
                || values.contains(where: { $0 == "关闭" || $0 == "close" || $0.contains("关闭") }) {
                let result = AXUIElementPerformAction(item, kAXPressAction as CFString)
                return ["ok": result == .success, "method": "AXPressClose", "code": result.rawValue]
            }
            stack.append(contentsOf: children(item))
        }
        if let rect = rectPayload(window) {
            let x = rect["x", default: 0] + 14
            let y = rect["y", default: 0] + 14
            clickAt(x: x, y: y)
            return ["ok": true, "method": "CGCloseButtonPoint", "x": x, "y": y, "windowCount": windows.count]
        }
    }
    return ["ok": false, "error": "close_button_not_found", "windowCount": windows.count]
}

func screenPayload() -> [String: Double] {
    guard let screen = NSScreen.main else {
        return [:]
    }
    let frame = screen.frame
    let visible = screen.visibleFrame
    return [
        "x": Double(frame.origin.x),
        "y": Double(frame.origin.y),
        "width": Double(frame.size.width),
        "height": Double(frame.size.height),
        "visibleX": Double(visible.origin.x),
        "visibleY": Double(visible.origin.y),
        "visibleWidth": Double(visible.size.width),
        "visibleHeight": Double(visible.size.height)
    ]
}

func mainWindow(_ app: AXUIElement) -> AXUIElement? {
    var focused: CFTypeRef?
    if AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &focused) == .success,
       let window = focused {
        return (window as! AXUIElement)
    }
    var value: CFTypeRef?
    if AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
       let windows = value as? [AXUIElement],
       let first = windows.first {
        return first
    }
    return nil
}

func conversationTables(root: AXUIElement, window: AXUIElement?) -> [AXUIElement] {
    var tables: [AXUIElement] = []
    if let window = window {
        collectTables(window, tables: &tables, maxDepth: 12)
    }
    if tables.isEmpty {
        collectTables(root, tables: &tables, maxDepth: 12)
    }
    return tables.filter { table in
        guard let rect = rectPayload(table),
              let screen = NSScreen.main else {
            return false
        }
        let screenWidth = Double(screen.frame.size.width)
        let rowCount = children(table).filter { role($0) == "AXRow" }.count
        return rowCount > 0
            && rect["width", default: 0] >= 160
            && rect["width", default: 0] <= max(520, screenWidth * 0.42)
            && rect["x", default: 0] <= max(360, screenWidth * 0.28)
    }
}

func conversationListTables(root: AXUIElement, window: AXUIElement?) -> [AXUIElement] {
    var tables: [AXUIElement] = []
    if let window = window {
        collectTables(window, tables: &tables, maxDepth: 12)
    }
    if tables.isEmpty {
        collectTables(root, tables: &tables, maxDepth: 12)
    }
    let navRects = navigationTables(root: root, window: window).compactMap { rectPayload($0) }
    let navRight = navRects.map { $0["x", default: 0] + $0["width", default: 0] }.max() ?? 0
    let windowRect = window.flatMap { rectPayload($0) }
    let maxConversationRight = windowRect.map { rect in
        rect["x", default: 0] + min(rect["width", default: 0] * 0.46, 760)
    } ?? 0
    return tables.filter { table in
        if looksLikeNavigationTable(table) {
            return false
        }
        guard let rect = rectPayload(table) else {
            return false
        }
        let tableTexts = collectText(table, maxDepth: 4)
        if tableTexts.contains("单聊") || tableTexts.contains("群聊") || tableTexts.contains("内部聊天") {
            return false
        }
        let rowCount = children(table).filter { role($0) == "AXRow" }.count
        let x = rect["x", default: 0]
        let width = rect["width", default: 0]
        return rowCount > 0
            && width >= 220
            && width <= 680
            && (navRight <= 0 || x >= navRight - 8)
            && (maxConversationRight <= 0 || x + width <= maxConversationRight)
    }.sorted { lhs, rhs in
        let lr = rectPayload(lhs) ?? [:]
        let rr = rectPayload(rhs) ?? [:]
        return lr["x", default: 0] < rr["x", default: 0]
    }
}

func conversationRows(root: AXUIElement, window: AXUIElement?) -> [AXUIElement] {
    var seen: Set<String> = []
    var out: [AXUIElement] = []
    for table in conversationTables(root: root, window: window) {
        for row in children(table) where role(row) == "AXRow" {
            let payload = rowPayload(row, index: out.count + 1)
            let texts = payload["texts"] as? [String] ?? []
            let width = payload["width"] as? Double ?? 0
            let height = payload["height"] as? Double ?? 0
            if texts.count < 2 || width < 160 || height < 20 {
                continue
            }
            let key = "\(payload["x"] ?? 0)|\(payload["y"] ?? 0)|\(texts.joined(separator: "|"))"
            if seen.contains(key) {
                continue
            }
            seen.insert(key)
            out.append(row)
        }
    }
    if !out.isEmpty {
        return out
    }
    var rows: [AXUIElement] = []
    collectRows(root, rows: &rows)
    return rows
}

func recentConversationRows(root: AXUIElement, window: AXUIElement?, limit: Int) -> [AXUIElement] {
    var seen: Set<String> = []
    var out: [AXUIElement] = []
    for table in conversationListTables(root: root, window: window) {
        for row in children(table) where role(row) == "AXRow" {
            let payload = rowPayload(row, index: out.count + 1)
            let texts = payload["texts"] as? [String] ?? []
            let width = payload["width"] as? Double ?? 0
            let height = payload["height"] as? Double ?? 0
            if texts.count < 2 || width < 220 || height < 20 {
                continue
            }
            let key = "\(payload["x"] ?? 0)|\(payload["y"] ?? 0)|\(texts.joined(separator: "|"))"
            if seen.contains(key) {
                continue
            }
            seen.insert(key)
            out.append(row)
            if out.count >= limit {
                return out
            }
        }
    }
    return out
}

func recentRowPayload(_ row: AXUIElement, index: Int, minutes: Int) -> [String: Any] {
    var payload = rowPayload(row, index: index)
    payload["timeText"] = rowTimeText(row)
    payload["hasUnreadMarker"] = rowHasUnreadMarker(row)
    payload["recentWindowMinutes"] = minutes
    payload["source"] = "axuielement-recent-rows"
    return payload
}

func selectedConversationRow(root: AXUIElement, window: AXUIElement?) -> AXUIElement? {
    func isNavigationRow(_ row: AXUIElement) -> Bool {
        let texts = collectText(row, maxDepth: 4).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        guard let first = texts.first else {
            return false
        }
        return first == "单聊" || first == "群聊" || first == "@我" || first == "未读" || first == "内部聊天"
    }
    for table in conversationListTables(root: root, window: window) {
        for row in children(table) where role(row) == "AXRow" {
            if isNavigationRow(row) {
                continue
            }
            let payload = rowPayload(row, index: 1)
            if payload["selected"] as? Bool ?? false {
                return row
            }
        }
    }
    for row in conversationRows(root: root, window: window) {
        if isNavigationRow(row) {
            continue
        }
        let payload = rowPayload(row, index: 1)
        if payload["selected"] as? Bool ?? false {
            return row
        }
    }
    return nil
}

func allRows(root: AXUIElement, window: AXUIElement?) -> [AXUIElement] {
    var rows: [AXUIElement] = []
    if let window = window {
        collectRows(window, rows: &rows, maxDepth: 14)
    }
    if rows.isEmpty {
        collectRows(root, rows: &rows, maxDepth: 14)
    }
    return rows
}

func chatPaneLeftBoundary(root: AXUIElement, window: AXUIElement?) -> Double? {
    let listRects = conversationListTables(root: root, window: window).compactMap { rectPayload($0) }
    if let right = listRects.map({ $0["x", default: 0] + $0["width", default: 0] }).max(), right > 0 {
        return right
    }
    let navRects = navigationTables(root: root, window: window).compactMap { rectPayload($0) }
    if let navRight = navRects.map({ $0["x", default: 0] + $0["width", default: 0] }).max(), navRight > 0 {
        return navRight
    }
    return nil
}

func rightSidebarLeftBoundary(root: AXUIElement, window: AXUIElement?) -> Double? {
    var webAreas: [AXUIElement] = []
    let target = window ?? root
    collectWebAreas(target, out: &webAreas, maxDepth: 14)
    let rects = webAreas.compactMap { rectPayload($0) }
    if rects.isEmpty {
        return nil
    }
    let leftMostSidebarX = rects.map { $0["x", default: 0] }.min() ?? 0
    return leftMostSidebarX > 0 ? leftMostSidebarX : nil
}

func chatRows(root: AXUIElement, window: AXUIElement?) -> [AXUIElement] {
    let rows = allRows(root: root, window: window)
    guard let chatLeft = chatPaneLeftBoundary(root: root, window: window) else {
        return rows
    }
    let windowRect = window.flatMap { rectPayload($0) } ?? [:]
    let fallbackRight = windowRect["x", default: 0] + max(windowRect["width", default: 0] - 240, 0)
    let sidebarLeft = rightSidebarLeftBoundary(root: root, window: window) ?? fallbackRight
    let tolerance = 4.0
    return rows.filter { row in
        guard let rect = rectPayload(row) else {
            return false
        }
        let x = rect["x", default: 0]
        let width = rect["width", default: 0]
        let right = x + width
        return width >= 240
            && x >= chatLeft - tolerance
            && (sidebarLeft <= 0 || right <= sidebarLeft + tolerance || x < sidebarLeft - tolerance)
    }
}

func setWindowFrame(_ window: AXUIElement, frame: NSRect) -> Bool {
    var pos = CGPoint(x: frame.origin.x, y: frame.origin.y)
    var size = CGSize(width: frame.size.width, height: frame.size.height)
    guard let posValue = AXValueCreate(.cgPoint, &pos),
          let sizeValue = AXValueCreate(.cgSize, &size) else {
        return false
    }
    let posResult = AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, posValue)
    let sizeResult = AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, sizeValue)
    return posResult == .success && sizeResult == .success
}

func boolValue(_ value: Bool) -> CFTypeRef {
    return (value ? kCFBooleanTrue : kCFBooleanFalse) as CFTypeRef
}

func settableTextInputs(_ element: AXUIElement, inputs: inout [AXUIElement], maxDepth: Int = 12, depth: Int = 0) {
    if depth > maxDepth {
        return
    }
    let elementRole = role(element)
    if elementRole == "AXTextArea" || elementRole == "AXTextField" {
        var settable = DarwinBoolean(false)
        let result = AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable)
        if result == .success && settable.boolValue {
            inputs.append(element)
        }
    }
    for child in children(element) {
        settableTextInputs(child, inputs: &inputs, maxDepth: maxDepth, depth: depth + 1)
    }
}

func inputScore(_ element: AXUIElement) -> Double {
    let pos = pointAttr(element, kAXPositionAttribute as CFString) ?? .zero
    let size = sizeAttr(element) ?? .zero
    var score = 0.0
    if pos.x > 300 { score += 1000 }
    if pos.y > 450 { score += 1000 }
    if size.width > 300 { score += 500 }
    if size.height > 20 { score += 100 }
    score += Double(pos.x) + Double(pos.y)
    return score
}

func sidebarRightBoundary(root: AXUIElement, window: AXUIElement?) -> Double? {
    return rightSidebarLeftBoundary(root: root, window: window)
}

func bestTextInput(root: AXUIElement, window: AXUIElement?) -> AXUIElement? {
    var inputs: [AXUIElement] = []
    if let window = window {
        settableTextInputs(window, inputs: &inputs)
    }
    if inputs.isEmpty {
        settableTextInputs(root, inputs: &inputs)
    }
    let windowRect = window.flatMap { rectPayload($0) } ?? [:]
    let winX = windowRect["x", default: 0]
    let winY = windowRect["y", default: 0]
    let winWidth = windowRect["width", default: 0]
    let winHeight = windowRect["height", default: 0]
    let sidebarLeft = sidebarRightBoundary(root: root, window: window) ?? (winWidth > 0 ? winX + winWidth * 0.82 : 0)
    let chatInputs = inputs.filter { input in
        guard let pos = pointAttr(input, kAXPositionAttribute as CFString),
              let size = sizeAttr(input) else {
            return false
        }
        let centerX = pos.x + size.width / 2
        let centerY = pos.y + size.height / 2
        let bottomMin = winHeight > 0 ? winY + winHeight * 0.72 : 0
        return size.width >= 260
            && size.height >= 40
            && (sidebarLeft <= 0 || centerX < sidebarLeft - 12)
            && (bottomMin <= 0 || centerY >= bottomMin)
    }
    if let input = chatInputs.max(by: { inputScore($0) < inputScore($1) }) {
        return input
    }
    return inputs.max(by: { inputScore($0) < inputScore($1) })
}

func ensureSidebarOpen(root: AXUIElement, window: AXUIElement?) -> [String: Any] {
    var textElements: [[String: Any]] = []
    let target = window ?? root
    collectTextElements(target, out: &textElements, maxDepth: 14)
    let hasSidebarText = textElements.contains { item in
        let texts = item["texts"] as? [String] ?? []
        return texts.contains { text in
            text.contains("工单工作台")
                || text.contains("AI客服")
                || text.contains("企微侧边栏")
                || text.contains("今日AI")
        }
    }
    if hasSidebarText {
        return ["ok": true, "alreadyOpen": true]
    }
    var buttons: [AXUIElement] = []
    if let window = window {
        collectButtons(window, buttons: &buttons)
    }
    if buttons.isEmpty {
        collectButtons(root, buttons: &buttons)
    }
    let openButton = buttons.first { button in
        let texts = textValues(button)
        return texts.contains { text in
            text.contains("打开侧边栏")
                || text.contains("展开")
                || text.contains("智能助手")
                || text.contains("AI客服")
                || text.contains("外部工具")
        }
    }
    guard let button = openButton else {
        return ["ok": false, "error": "sidebar_open_button_not_found", "alreadyOpen": false]
    }
    let result = AXUIElementPerformAction(button, kAXPressAction as CFString)
    if result != .success, let center = buttonCenter(button) {
        clickAt(x: Double(center.x), y: Double(center.y))
    }
    Thread.sleep(forTimeInterval: 0.25)
    return ["ok": true, "alreadyOpen": false, "code": result.rawValue]
}

func inputReadyPayload(root: AXUIElement, window: AXUIElement?) -> [String: Any] {
    let sidebar = ensureSidebarOpen(root: root, window: window)
    guard let input = bestTextInput(root: root, window: window) else {
        return ["ok": false, "error": "chat_input_not_found", "sidebar": sidebar]
    }
    activateRunningApp(bundleID: bundleID)
    AXUIElementSetAttributeValue(root, kAXFocusedUIElementAttribute as CFString, input)
    AXUIElementSetAttributeValue(input, kAXFocusedAttribute as CFString, boolValue(true))
    if let pos = pointAttr(input, kAXPositionAttribute as CFString),
       let size = sizeAttr(input) {
        clickAt(x: Double(pos.x + min(max(size.width - 16, 10), size.width / 2)), y: Double(pos.y + size.height / 2))
    }
    Thread.sleep(forTimeInterval: 0.08)
    let pos = pointAttr(input, kAXPositionAttribute as CFString) ?? .zero
    let size = sizeAttr(input) ?? .zero
    return [
        "ok": true,
        "sidebar": sidebar,
        "input": [
            "x": Double(pos.x),
            "y": Double(pos.y),
            "width": Double(size.width),
            "height": Double(size.height),
            "valueLength": (stringAttr(input, kAXValueAttribute as CFString) ?? "").count
        ]
    ]
}

func collectWebAreas(_ element: AXUIElement, out: inout [AXUIElement], maxDepth: Int = 14, depth: Int = 0) {
    if depth > maxDepth {
        return
    }
    if role(element) == "AXWebArea" {
        out.append(element)
    }
    for child in children(element) {
        collectWebAreas(child, out: &out, maxDepth: maxDepth, depth: depth + 1)
    }
}

func sidebarIdentityPayload(root: AXUIElement, window: AXUIElement?) -> [String: Any] {
    var webAreas: [AXUIElement] = []
    let target = window ?? root
    collectWebAreas(target, out: &webAreas, maxDepth: 14)
    if webAreas.isEmpty {
        return ["ok": false, "error": "sidebar_webarea_not_found", "external_user_id": ""]
    }
    let sorted = webAreas.sorted { lhs, rhs in
        let lr = rectPayload(lhs) ?? [:]
        let rr = rectPayload(rhs) ?? [:]
        return lr["x", default: 0] > rr["x", default: 0]
    }
    guard let sidebar = sorted.first else {
        return ["ok": false, "error": "sidebar_webarea_not_found", "external_user_id": ""]
    }
    let texts = collectText(sidebar, maxDepth: 10)
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }
    let uidRegex = try? NSRegularExpression(pattern: #"\b(w[mo][A-Za-z0-9_-]{8,})\b"#)
    var uid = ""
    for text in texts {
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        if let match = uidRegex?.firstMatch(in: text, range: range),
           let swiftRange = Range(match.range(at: 1), in: text) {
            uid = String(text[swiftRange])
            break
        }
    }
    return [
        "ok": true,
        "external_user_id": uid,
        "texts": Array(texts.prefix(120)),
        "textCount": texts.count,
        "webArea": rectPayload(sidebar) ?? [:]
    ]
}

func intArg(_ index: Int, defaultValue: Int) -> Int {
    if CommandLine.arguments.count > index, let value = Int(CommandLine.arguments[index]) {
        return value
    }
    return defaultValue
}

let args = CommandLine.arguments
let command = args.count > 1 ? args[1] : "rows"
let bundleID = ProcessInfo.processInfo.environment["WECOM_GUI_BUNDLE_ID"] ?? "com.tencent.WeWorkMac"
guard let root = appElement(bundleID: bundleID) else {
    fputs("app not running: \(bundleID)\n", stderr)
    exit(1)
}

let window = mainWindow(root)

if command == "rows" {
    let rows = conversationRows(root: root, window: window)
    for (index, row) in rows.enumerated() {
        let payload = rowPayload(row, index: index + 1)
        let texts = payload["texts"] as? [String] ?? []
        if texts.count >= 2 {
            jsonLine(payload)
        }
    }
} else if command == "ensure-single-chat" {
    jsonLine(ensureSingleChat(root: root, window: window))
} else if command == "recent-rows" {
    let minutes = max(1, intArg(2, defaultValue: 10))
    let limit = max(1, intArg(3, defaultValue: 12))
    _ = ensureSingleChat(root: root, window: window)
    let recentRows = recentConversationRows(root: root, window: window, limit: limit)
    for (index, row) in recentRows.enumerated() {
        let payload = recentRowPayload(row, index: index + 1, minutes: minutes)
        let texts = payload["texts"] as? [String] ?? []
        if texts.count >= 2 {
            jsonLine(payload)
        }
    }
} else if command == "selected-row" {
    if let row = selectedConversationRow(root: root, window: window) {
        var payload = rowPayload(row, index: 1)
        payload["timeText"] = rowTimeText(row)
        payload["hasUnreadMarker"] = rowHasUnreadMarker(row)
        payload["source"] = "axuielement-selected-row"
        jsonLine(payload)
    } else {
        jsonLine(["ok": false, "error": "selected_conversation_not_found", "source": "axuielement-selected-row"])
    }
} else if command == "input-ready" {
    jsonLine(inputReadyPayload(root: root, window: window))
} else if command == "sidebar-identity" {
    jsonLine(sidebarIdentityPayload(root: root, window: window))
} else if command == "chat" || command == "chat-all" {
    let rows = chatRows(root: root, window: window)
    for (index, row) in rows.enumerated() {
        let payload = chatPayload(row, index: index + 1)
        let texts = payload["texts"] as? [String] ?? []
        let containsMessageLikeText = texts.contains { text in
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return !trimmed.isEmpty && trimmed != "个人名片" && trimmed != "以上是打招呼内容"
        }
        let mediaElements = payload["mediaElements"] as? [[String: Any]] ?? []
        if command == "chat-all" || containsMessageLikeText || !mediaElements.isEmpty {
            jsonLine(payload)
        }
    }
} else if command == "texts" {
    for text in collectText(root, maxDepth: 14) {
        jsonLine(["text": text])
    }
} else if command == "elements" {
    let target = window ?? root
    var textElements: [[String: Any]] = []
    collectTextElements(target, out: &textElements, maxDepth: 14)
    var mediaElements: [[String: Any]] = []
    collectMediaElements(target, out: &mediaElements, maxDepth: 14)
    for item in textElements {
        var payload = item
        payload["kind"] = "text"
        jsonLine(payload)
    }
    for item in mediaElements {
        var payload = item
        payload["kind"] = "media"
        jsonLine(payload)
    }
} else if command == "geometry" {
    let rows = conversationRows(root: root, window: window)
    let tables = conversationTables(root: root, window: window)
    let listTables = conversationListTables(root: root, window: window)
    var sidebar = tables.compactMap { rectPayload($0) }.first ?? [:]
    let conversationList = listTables.compactMap { rectPayload($0) }.first ?? [:]
    if sidebar.isEmpty, let firstRow = rows.first, let firstRect = rectPayload(firstRow) {
        let rowHeight = firstRect["height", default: 56]
        let rowCount = max(1, min(rows.count, 12))
        sidebar = [
            "x": firstRect["x", default: 0],
            "y": firstRect["y", default: 0],
            "width": firstRect["width", default: 250],
            "height": max(firstRect["height", default: 56], rowHeight * Double(rowCount))
        ]
    }
    let scrollX = sidebar["x", default: 0] + sidebar["width", default: 0] * 0.5
    let scrollY = sidebar["y", default: 0] + sidebar["height", default: 0] * 0.55
    var payload: [String: Any] = [
        "ok": true,
        "screen": screenPayload(),
        "window": window.flatMap { rectPayload($0) } ?? [:],
        "sidebar": sidebar,
        "conversationList": conversationList,
        "chatLeft": chatPaneLeftBoundary(root: root, window: window) ?? 0,
        "rightSidebarLeft": rightSidebarLeftBoundary(root: root, window: window) ?? 0,
        "scrollPoint": ["x": scrollX, "y": scrollY],
        "rowCount": rows.count,
        "source": sidebar.isEmpty ? "ax-rows" : "ax-table"
    ]
    if let firstRow = rows.first, let rect = rectPayload(firstRow) {
        payload["firstRow"] = rect
    }
    jsonLine(payload)
} else if command == "normalize" {
    guard let window = window else {
        fputs("window not found\n", stderr)
        exit(1)
    }
    let mode = args.count > 2 ? args[2] : "fullscreen"
    var fullscreen = false
    var fallbackMaximized = false
    if mode == "fullscreen" {
        let setResult = AXUIElementSetAttributeValue(window, "AXFullScreen" as CFString, boolValue(true))
        Thread.sleep(forTimeInterval: 0.7)
        var value: CFTypeRef?
        if setResult == .success,
           AXUIElementCopyAttributeValue(window, "AXFullScreen" as CFString, &value) == .success {
            fullscreen = (value as? Bool) ?? false
        }
    }
    if !fullscreen {
        if let screen = NSScreen.main {
            fallbackMaximized = setWindowFrame(window, frame: screen.visibleFrame)
            Thread.sleep(forTimeInterval: 0.2)
        }
    }
    jsonLine([
        "ok": true,
        "fullscreen": fullscreen,
        "fallbackMaximized": fallbackMaximized,
        "window": rectPayload(window) ?? [:],
        "screen": screenPayload()
    ])
} else if command == "open" {
    let rows = conversationRows(root: root, window: window)
    let target = args.dropFirst(2).joined(separator: " ")
    if target.isEmpty {
        fputs("missing target\n", stderr)
        exit(2)
    }
    for (index, row) in rows.enumerated() {
        let payload = rowPayload(row, index: index + 1)
        let texts = payload["texts"] as? [String] ?? []
        if texts.contains(where: { $0 == target || $0.hasPrefix(target + " ") }) {
            AXUIElementSetAttributeValue(row, kAXSelectedAttribute as CFString, boolValue(true))
            AXUIElementPerformAction(row, kAXPressAction as CFString)
            jsonLine(["ok": true, "target": target, "index": index + 1])
            exit(0)
        }
    }
    fputs("row not found: \(target)\n", stderr)
    exit(1)
} else if command == "scroll" {
    let rows = conversationRows(root: root, window: window)
    let direction = args.count > 2 ? args[2] : "down"
    let ticks = max(1, intArg(3, defaultValue: 6))
    var x = Double(intArg(4, defaultValue: -1))
    var y = Double(intArg(5, defaultValue: -1))
    if x < 0 || y < 0 {
        let tables = conversationTables(root: root, window: window)
        if let sidebar = tables.compactMap({ rectPayload($0) }).first {
            x = sidebar["x", default: 0] + sidebar["width", default: 0] * 0.5
            y = sidebar["y", default: 0] + sidebar["height", default: 0] * 0.55
        } else if let first = rows.first, let rowRect = rectPayload(first) {
            x = rowRect["x", default: 0] + rowRect["width", default: 0] * 0.5
            y = rowRect["y", default: 0] + rowRect["height", default: 0] * 0.5
        } else {
            fputs("sidebar frame not found\n", stderr)
            exit(1)
        }
    }
    let amount = direction == "up" ? ticks : -ticks
    let source = CGEventSource(stateID: .hidSystemState)
    let point = CGPoint(x: x, y: y)
    CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
    CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 1, wheel1: Int32(amount), wheel2: 0, wheel3: 0)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.2)
    jsonLine(["ok": true, "direction": direction, "ticks": ticks, "x": x, "y": y])
} else if command == "doubleclick" {
    let x = Double(intArg(2, defaultValue: -1))
    let y = Double(intArg(3, defaultValue: -1))
    if x < 0 || y < 0 {
        jsonLine(["ok": false, "error": "invalid_point"])
        exit(0)
    }
    doubleClickAt(x: x, y: y)
    jsonLine(["ok": true, "x": x, "y": y, "method": "cg_double_click"])
} else if command == "preview" {
    jsonLine(previewPayload(root: root))
} else if command == "close-preview" {
    jsonLine(closePreview(root: root))
} else if command == "send" || command == "stage" {
    let text = args.dropFirst(2).joined(separator: " ")
    if text.isEmpty {
        jsonLine(["ok": false, "error": "missing_text"])
        exit(0)
    }
    guard let input = bestTextInput(root: root, window: window) else {
        jsonLine(["ok": false, "error": "chat_input_not_found"])
        exit(0)
    }
    let setResult = AXUIElementSetAttributeValue(input, kAXValueAttribute as CFString, text as CFTypeRef)
    if setResult != .success {
        jsonLine(["ok": false, "error": "set_input_failed", "code": setResult.rawValue])
        exit(0)
    }
    activateRunningApp(bundleID: bundleID)
    AXUIElementSetAttributeValue(root, kAXFocusedUIElementAttribute as CFString, input)
    AXUIElementSetAttributeValue(input, kAXFocusedAttribute as CFString, boolValue(true))
    Thread.sleep(forTimeInterval: 0.15)
    var submitResult: [String: Any] = ["method": "stage_only"]
    if command == "send" {
        submitResult = submitInput(root: root, window: window, input: input, text: text)
    }
    let pos = pointAttr(input, kAXPositionAttribute as CFString) ?? .zero
    let size = sizeAttr(input) ?? .zero
    jsonLine([
        "ok": true,
        "submitted": command == "send",
        "chars": text.count,
        "method": submitResult["method"] ?? "ax_text_input",
        "submit": submitResult,
        "x": Double(pos.x),
        "y": Double(pos.y),
        "width": Double(size.width),
        "height": Double(size.height)
    ])
} else {
    fputs("unknown command: \(command)\n", stderr)
    exit(2)
}
