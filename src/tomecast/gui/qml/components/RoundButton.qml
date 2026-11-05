// The 56 px round play/pause button (voice preview, pause converting). Icons are drawn shapes.
import QtQuick
import QtQuick.Shapes
import QtQuick.Templates as T
import ".."

T.AbstractButton {
    id: control

    // "play" or "pause"
    property string glyph: "play"
    // 56 px, or 44 px in the corner window (with a 14 px glyph).
    property bool compact: false

    implicitWidth: compact ? 44 : Theme.roundButtonSize
    implicitHeight: implicitWidth
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    opacity: enabled ? 1 : 0.4

    Accessible.role: Accessible.Button
    Accessible.name: text
    Accessible.onPressAction: control.clicked()

    background: Rectangle {
        radius: width / 2
        color: control.down ? Theme.line : "transparent"
        border.width: 1
        border.color: Theme.text

        FocusRing {
            target: control
            cornerRadius: parent.radius
        }
    }

    contentItem: Item {
        Shape {
            id: glyphShape
            width: 16
            height: 16
            scale: control.compact ? 14 / 16 : 1
            anchors.centerIn: parent
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                strokeWidth: -1
                strokeColor: "transparent"
                fillColor: Theme.text
                PathSvg {
                    path: control.glyph === "pause"
                          ? "M 3 2 L 6.5 2 L 6.5 14 L 3 14 Z M 9.5 2 L 13 2 L 13 14 L 9.5 14 Z"
                          : "M 4 2 L 14 8 L 4 14 Z"
                }
            }
        }
    }
}
