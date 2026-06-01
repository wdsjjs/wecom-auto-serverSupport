import Foundation
import Vision
import AppKit

if CommandLine.arguments.count < 2 {
    fputs("usage: vision_ocr.swift IMAGE_PATH\n", stderr)
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: imageURL),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("failed to load image\n", stderr)
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fputs("ocr failed: \(error)\n", stderr)
    exit(1)
}

let width = Double(cgImage.width)
let height = Double(cgImage.height)
let observations = (request.results ?? []).compactMap { observation -> String? in
    guard let candidate = observation.topCandidates(1).first else {
        return nil
    }
    let box = observation.boundingBox
    let x = box.origin.x * width
    let y = (1.0 - box.origin.y - box.height) * height
    let w = box.width * width
    let h = box.height * height
    let text = candidate.string.replacingOccurrences(of: "\n", with: " ")
    return "\(x)\t\(y)\t\(w)\t\(h)\t\(text)"
}

print(observations.joined(separator: "\n"))
