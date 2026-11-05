// IBM Plex Mono label: caps data and labels, with slight tracking (in em).
import QtQuick
import ".."

Text {
    // Pixel size and letter spacing (em). Use these, not font.pixelSize, so the tracking
    // follows the size.
    property int size: 11
    property real tracking: 0.12

    font.family: Theme.mono
    font.pixelSize: size
    font.letterSpacing: size * tracking
    color: Theme.muted
    textFormat: Text.PlainText
}
