// Renders the 1024×1024 app icon (opaque, as App Store requires). Usage: swift scripts/make-icon.swift <out.png>
// A white story card on a black field, struck through by a lightning bolt: news, short and fast.
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let size = 1024
let space = CGColorSpaceCreateDeviceRGB()
let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
                    space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor { CGColor(red: r, green: g, blue: b, alpha: a) }

// Field: near black, with a soft red glow behind the card; the back card is red.
let bg = CGGradient(colorsSpace: space, colors: [rgb(0.04, 0.04, 0.05), rgb(0.13, 0.10, 0.11)] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 0, y: 0), end: CGPoint(x: 1024, y: 1024), options: [])
let glow = CGGradient(colorsSpace: space, colors: [rgb(0.95, 0.20, 0.18, 0.38), rgb(0.95, 0.20, 0.18, 0)] as CFArray, locations: [0, 1])!
ctx.drawRadialGradient(glow, startCenter: CGPoint(x: 512, y: 512), startRadius: 0, endCenter: CGPoint(x: 512, y: 512), endRadius: 560, options: [])

func cardPath(_ rect: CGRect) -> CGPath { CGPath(roundedRect: rect, cornerWidth: 84, cornerHeight: 84, transform: nil) }
let rect = CGRect(x: -262, y: -330, width: 524, height: 660)

// Back card.
ctx.saveGState()
ctx.translateBy(x: 512, y: 512); ctx.rotate(by: 0.20)
ctx.addPath(cardPath(rect)); ctx.setFillColor(rgb(0.93, 0.20, 0.18, 0.85)); ctx.fillPath()
ctx.restoreGState()

// Front card, white, with a shadow.
ctx.saveGState()
ctx.translateBy(x: 512, y: 512); ctx.rotate(by: -0.06)
ctx.setShadow(offset: CGSize(width: 0, height: -24), blur: 60, color: rgb(0.25, 0, 0, 0.40))
ctx.addPath(cardPath(rect)); ctx.setFillColor(rgb(1, 1, 1)); ctx.fillPath()
ctx.setShadow(offset: .zero, blur: 0, color: nil)

// Lightning bolt in the brand gradient, clipped so the gradient fills the bolt.
let bolt = CGMutablePath()
let pts: [(CGFloat, CGFloat)] = [(70, 250), (-175, -48), (-18, -48), (-70, -250), (175, 48), (18, 48)]
bolt.move(to: CGPoint(x: pts[0].0, y: pts[0].1))
for p in pts.dropFirst() { bolt.addLine(to: CGPoint(x: p.0, y: p.1)) }
bolt.closeSubpath()
let brand = CGGradient(colorsSpace: space, colors: [rgb(0.98, 0.30, 0.20), rgb(0.74, 0.07, 0.18)] as CFArray, locations: [0, 1])!
// Twice: the bolt itself, then its outline stroked wide, which rounds the corners.
for outline in [false, true] {
    ctx.saveGState()
    ctx.addPath(bolt)
    if outline { ctx.setLineJoin(.round); ctx.setLineWidth(44); ctx.replacePathWithStrokedPath() }
    ctx.clip()
    ctx.drawLinearGradient(brand, start: CGPoint(x: -170, y: 290), end: CGPoint(x: 170, y: -290), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
    ctx.restoreGState()
}
ctx.restoreGState()

let out = URL(fileURLWithPath: CommandLine.arguments[1])
let dest = CGImageDestinationCreateWithURL(out as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(dest, ctx.makeImage()!, nil)
CGImageDestinationFinalize(dest)
