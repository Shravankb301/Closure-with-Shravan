import AppKit
import Foundation
import PDFKit
import Vision

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("Usage: macos_vision_ocr.swift <pdf>\n".utf8))
    exit(2)
}

let sourceURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let document = PDFDocument(url: sourceURL) else {
    FileHandle.standardError.write(Data("Unable to open PDF\n".utf8))
    exit(3)
}

for pageIndex in 0..<document.pageCount {
    autoreleasepool {
        guard let page = document.page(at: pageIndex) else { return }
        let bounds = page.bounds(for: .mediaBox)
        let scale: CGFloat = 2.0
        let width = max(1, Int(bounds.width * scale))
        let height = max(1, Int(bounds.height * scale))
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return }

        context.setFillColor(NSColor.white.cgColor)
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        context.saveGState()
        context.scaleBy(x: scale, y: scale)
        page.draw(with: .mediaBox, to: context)
        context.restoreGState()
        guard let image = context.makeImage() else { return }

        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.recognitionLanguages = ["en-US"]
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        do {
            try handler.perform([request])
            let observations = (request.results ?? []).sorted {
                let verticalDifference = $0.boundingBox.maxY - $1.boundingBox.maxY
                if abs(verticalDifference) > 0.01 { return verticalDifference > 0 }
                return $0.boundingBox.minX < $1.boundingBox.minX
            }
            print("\u{001e}PAGE:\(pageIndex + 1)\u{001e}")
            for observation in observations {
                if let candidate = observation.topCandidates(1).first {
                    print(candidate.string)
                }
            }
        } catch {
            FileHandle.standardError.write(Data("OCR failed on page \(pageIndex + 1): \(error)\n".utf8))
        }
    }
}
