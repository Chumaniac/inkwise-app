// Local Apple Vision OCR adapter. It never performs language correction.
import Foundation
import Vision
import ImageIO

guard CommandLine.arguments.count == 2 else { exit(2) }
let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
do {
    let supported = try request.supportedRecognitionLanguages()
    request.recognitionLanguages = ["zh-Hans", "en-US"].filter { supported.contains($0) }
    let handler = VNImageRequestHandler(url: imageURL, options: [:])
    try handler.perform([request])
    let observations = (request.results ?? []).sorted {
        if abs($0.boundingBox.midY - $1.boundingBox.midY) < 0.015 {
            return $0.boundingBox.minX < $1.boundingBox.minX
        }
        return $0.boundingBox.midY > $1.boundingBox.midY
    }
    var lines: [[String: Any]] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        lines.append([
            "text": candidate.string, "confidence": candidate.confidence,
            "box": [box.minX, 1.0 - box.maxY, box.width, box.height],
            "alternatives": observation.topCandidates(3).map { $0.string }
        ])
    }
    let result: [String: Any] = ["engine": "apple-vision", "revision": request.revision,
                                "lines": lines, "requiresReview": true]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
} catch {
    // Do not print image paths, recognized text or underlying private errors.
    FileHandle.standardError.write(Data("OCR_FAILED\n".utf8))
    exit(1)
}
