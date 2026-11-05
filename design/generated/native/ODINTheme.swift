// Generated from design/themes/foundry/tokens.json. Do not edit.
import SwiftUI

public enum ODINTheme {
    public enum Foundry {
        // Pass the bundle containing Foundry.xcassets (app or package).
        public struct Palette {
            public let bundle: Bundle
            public init(bundle: Bundle = .main) { self.bundle = bundle }
            public var surfaceGround: Color { Color("FoundrySurfaceGround", bundle: bundle) }
            public var surfacePanel: Color { Color("FoundrySurfacePanel", bundle: bundle) }
            public var surfaceRaised: Color { Color("FoundrySurfaceRaised", bundle: bundle) }
            public var surfaceBorder: Color { Color("FoundrySurfaceBorder", bundle: bundle) }
            public var textPrimary: Color { Color("FoundryTextPrimary", bundle: bundle) }
            public var textSecondary: Color { Color("FoundryTextSecondary", bundle: bundle) }
            public var textMuted: Color { Color("FoundryTextMuted", bundle: bundle) }
            public var actionPrimary: Color { Color("FoundryActionPrimary", bundle: bundle) }
            public var actionOnPrimary: Color { Color("FoundryActionOnPrimary", bundle: bundle) }
            public var statusNeutralText: Color { Color("FoundryStatusNeutralText", bundle: bundle) }
            public var statusNeutralBackground: Color { Color("FoundryStatusNeutralBackground", bundle: bundle) }
            public var statusNeutralForeground: Color { Color("FoundryStatusNeutralForeground", bundle: bundle) }
            public var statusPrintingText: Color { Color("FoundryStatusPrintingText", bundle: bundle) }
            public var statusPrintingBackground: Color { Color("FoundryStatusPrintingBackground", bundle: bundle) }
            public var statusPrintingForeground: Color { Color("FoundryStatusPrintingForeground", bundle: bundle) }
            public var statusReadyText: Color { Color("FoundryStatusReadyText", bundle: bundle) }
            public var statusReadyBackground: Color { Color("FoundryStatusReadyBackground", bundle: bundle) }
            public var statusReadyForeground: Color { Color("FoundryStatusReadyForeground", bundle: bundle) }
            public var statusNeedsYouText: Color { Color("FoundryStatusNeedsYouText", bundle: bundle) }
            public var statusNeedsYouBackground: Color { Color("FoundryStatusNeedsYouBackground", bundle: bundle) }
            public var statusNeedsYouForeground: Color { Color("FoundryStatusNeedsYouForeground", bundle: bundle) }
            public var statusQueuedText: Color { Color("FoundryStatusQueuedText", bundle: bundle) }
            public var statusQueuedBackground: Color { Color("FoundryStatusQueuedBackground", bundle: bundle) }
            public var statusQueuedForeground: Color { Color("FoundryStatusQueuedForeground", bundle: bundle) }
            public var statusWaitingOnYouText: Color { Color("FoundryStatusWaitingOnYouText", bundle: bundle) }
            public var statusWaitingOnYouBackground: Color { Color("FoundryStatusWaitingOnYouBackground", bundle: bundle) }
            public var statusWaitingOnYouForeground: Color { Color("FoundryStatusWaitingOnYouForeground", bundle: bundle) }
        }
        public enum Radius {
            public static let card: CGFloat = 12
            public static let control: CGFloat = 8
            public static let chip: CGFloat = 6
        }
        public enum Spacing {
            public static let grid: CGFloat = 4
            public static let comfortable: CGFloat = 20
            public static let compact: CGFloat = 12
            public static let touchTarget: CGFloat = 44
            public static let pointerTarget: CGFloat = 36
        }
        // Phase 3 must bundle the Plex font files in the native target.
        public enum Typography {
            public static let display = Font.custom("IBMPlexSans-SemiBold", size: 32, relativeTo: .largeTitle)
            public static let title = Font.custom("IBMPlexSans-SemiBold", size: 20, relativeTo: .title2)
            public static let body = Font.custom("IBMPlexSans", size: 15, relativeTo: .body)
            public static let mono = Font.custom("IBMPlexMono", size: 15, relativeTo: .body).monospacedDigit()
            public static let minimum: CGFloat = 13
        }
    }
}
