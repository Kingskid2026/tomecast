// The round play/pause button (voice preview, pause converting, the Names screen's players):
// 56 px, or 44 px with a 12 px icon in the Names table. Icons are drawn shapes.
import QtQuick
import QtQuick.Shapes
import QtQuick.Templates as T
import ".."

T.AbstractButton {
    id: control

    // "play" or "pause"
    property string glyph: "play"
    // The outline and icon colour: the text colour, or the accent while a Names row plays.
    property color ink: Theme.text
    property real size: Theme.roundButtonSize
    property real glyphSize: 16

    implicitWidth: size
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
        border.color: control.ink

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
            anchors.centerIn: parent
            scale: control.glyphSize / 16
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                strokeWidth: -1
                strokeColor: "transparent"
                fillColor: control.ink
                PathSvg {
                    path: control.glyph === "pause"
                          ? "M 3 2 L 6.5 2 L 6.5 14 L 3 14 Z M 9.5 2 L 13 2 L 13 14 L 9.5 14 Z"
                          : "M 4 2 L 14 8 L 4 14 Z"
                }
            }
        }
    }
}
