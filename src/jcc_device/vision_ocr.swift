// Local, persistent Apple Vision OCR worker. Images and results stay on this Mac.
import Foundation
import ImageIO
import Vision

struct OCRBox: Encodable {
    let text: String
    let confidence: Float
    let x: Int
    let y: Int
    let width: Int
    let height: Int
}

struct OCRResponse: Encodable {
    let results: [OCRBox]?
    let error: String?
}

func readExactly(_ count: Int) -> Data? {
    var result = Data()
    while result.count < count {
        let part = FileHandle.standardInput.readData(ofLength: count - result.count)
        if part.isEmpty { return nil }
        result.append(part)
    }
    return result
}

func recognize(_ data: Data) throws -> [OCRBox] {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "JCCVision", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "Invalid image"])
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    let available = try request.supportedRecognitionLanguages()
    guard available.contains("zh-Hans") else {
        throw NSError(domain: "JCCVision", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "Simplified Chinese OCR is unavailable"])
    }
    request.recognitionLanguages = ["zh-Hans"] + (available.contains("en-US") ? ["en-US"] : [])
    request.minimumTextHeight = 0.005
    try VNImageRequestHandler(cgImage: image).perform([request])
    return (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return OCRBox(text: candidate.string, confidence: candidate.confidence,
                      x: Int((box.minX * Double(image.width)).rounded()),
                      y: Int(((1 - box.maxY) * Double(image.height)).rounded()),
                      width: Int((box.width * Double(image.width)).rounded()),
                      height: Int((box.height * Double(image.height)).rounded()))
    }
}

let encoder = JSONEncoder()
while let header = readExactly(4) {
    let length = header.reduce(0) { ($0 << 8) | Int($1) }
    guard length > 0 && length <= 25 * 1024 * 1024, let data = readExactly(length) else { break }
    let response: OCRResponse
    do {
        response = OCRResponse(results: try recognize(data), error: nil)
    } catch {
        response = OCRResponse(results: nil, error: error.localizedDescription)
    }
    if let payload = try? encoder.encode(response) {
        FileHandle.standardOutput.write(payload)
        FileHandle.standardOutput.write(Data([10]))
    }
}
