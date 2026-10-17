import Foundation
import Vision
import AppKit

func probe(_ path: String) -> [String: Any] {
    var result: [String: Any] = ["path": path]
    guard let image = NSImage(contentsOfFile: path),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        result["error"] = "unreadable"
        return result
    }
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])

    let classify = VNClassifyImageRequest()
    do {
        try handler.perform([classify])
        let labels = (classify.results ?? [])
            .filter { $0.confidence >= 0.05 }
            .prefix(20)
            .map { [$0.identifier, $0.confidence] as [Any] }
        result["labels"] = labels
    } catch {
        result["error"] = "classify: \(error.localizedDescription)"
    }

    let ocr = VNRecognizeTextRequest()
    ocr.recognitionLevel = .fast
    ocr.usesLanguageCorrection = false
    ocr.recognitionLanguages = ["en-US", "zh-Hans"]
    do {
        try handler.perform([ocr])
        var lines = 0
        var chars = 0
        var area = 0.0
        var confidence = 0.0
        for observation in ocr.results ?? [] {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.count < 2 { continue }
            lines += 1
            chars += text.count
            let box = observation.boundingBox
            area += Double(box.width * box.height)
            confidence += Double(candidate.confidence)
        }
        result["textLines"] = lines
        result["textChars"] = chars
        result["textArea"] = min(1.0, area)
        result["textConfidence"] = lines > 0 ? confidence / Double(lines) : 0.0
    } catch {
        result["error"] = "ocr: \(error.localizedDescription)"
    }
    return result
}

var paths: [String] = []
for argument in CommandLine.arguments.dropFirst() {
    if argument == "--stdin" {
        while let line = readLine() {
            let path = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if !path.isEmpty { paths.append(path) }
        }
    } else {
        paths.append(argument)
    }
}

for path in paths {
    autoreleasepool {
        let payload = probe(path)
        if let data = try? JSONSerialization.data(withJSONObject: payload),
           let line = String(data: data, encoding: .utf8) {
            print(line)
            fflush(stdout)
        }
    }
}
