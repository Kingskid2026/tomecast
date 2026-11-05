// A tick box (the visual part of a checkbox): accent with a check when on. 18 px, or 16 px
// in the corner window.
import QtQuick
import QtQuick.Shapes
import ".."

Rectangle {
    id: box

    property bool checked: false
    property int size: 18
    readonly property real k: size / 18

    implicitWidth: size
    implicitHeight: size
    radius: 3
    color: checked ? Theme.accent : "transparent"
    border.width: checked ? 0 : 1
    border.color: Theme.muted
    Accessible.ignored: true

    Shape {
        anchors.fill: parent
        visible: box.checked
        preferredRendererType: Shape.CurveRenderer

        ShapePath {
            strokeColor: Theme.textOnAccent
            strokeWidth: 2
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            startX: 4.5 * box.k
            startY: 9.5 * box.k
            PathLine { x: 7.75 * box.k; y: 12.75 * box.k }
            PathLine { x: 13.5 * box.k; y: 5.5 * box.k }
        }
    }
}
