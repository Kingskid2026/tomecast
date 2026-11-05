// A big numeral or timer in Doto (24 px and up only), set solid like CSS line-height: 1:
// the box is exactly `size` tall and the glyphs sit centred in it, as in the mockups.
import QtQuick
import ".."

Item {
    id: root

    property alias text: label.text
    property int size: 64

    implicitWidth: label.implicitWidth
    implicitHeight: size
    baselineOffset: label.y + label.baselineOffset

    Accessible.role: Accessible.StaticText
    Accessible.name: text

    Text {
        id: label
        y: Math.round((root.height - implicitHeight) / 2)
        color: Theme.text
        font.family: Theme.display
        font.weight: Font.Bold
        font.pixelSize: root.size
        font.letterSpacing: root.size * 0.02
        textFormat: Text.PlainText
        Accessible.ignored: true
    }
}
