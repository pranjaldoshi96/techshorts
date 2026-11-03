// Renders the 1024×1024 app icon (opaque, as App Store requires). Usage: swift scripts/make-icon.swift <out.png>
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let size = 1024
let space = CGColorSpaceCreateDeviceRGB()
let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
                    space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor { CGColor(red: r, green: g, blue: b, alpha: a) }

let bg = CGGradient(colorsSpace: space, colors: [rgb(0.18, 0.42, 1), rgb(0.56, 0.30, 0.98)] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 0, y: 1024), end: CGPoint(x: 1024, y: 0), options: [])

func card(angle: CGFloat, fill: CGColor, contents: Bool) {
    ctx.saveGState()
    ctx.translateBy(x: 512, y: 500)
    ctx.rotate(by: angle)
    let rect = CGRect(x: -250, y: -310, width: 500, height: 620)
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: 60, cornerHeight: 60, transform: nil))
    ctx.setFillColor(fill)
    ctx.fillPath()
    if contents {
        ctx.addPath(CGPath(roundedRect: CGRect(x: -200, y: 50, width: 400, height: 200), cornerWidth: 30, cornerHeight: 30, transform: nil))
        ctx.setFillColor(rgb(0.18, 0.42, 1))
        ctx.fillPath()
        for (y, w) in [(-20.0, 400.0), (-90.0, 400.0), (-160.0, 400.0), (-230.0, 250.0)] {
            ctx.addPath(CGPath(roundedRect: CGRect(x: -200, y: y, width: w, height: 34), cornerWidth: 17, cornerHeight: 17, transform: nil))
            ctx.setFillColor(rgb(0.80, 0.82, 0.88))
            ctx.fillPath()
        }
    }
    ctx.restoreGState()
}
card(angle: 0.16, fill: rgb(1, 1, 1, 0.35), contents: false)
card(angle: -0.05, fill: rgb(1, 1, 1), contents: true)

let out = URL(fileURLWithPath: CommandLine.arguments[1])
let dest = CGImageDestinationCreateWithURL(out as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(dest, ctx.makeImage()!, nil)
CGImageDestinationFinalize(dest)
