// A row of n dots with k lit: chapter length, the preview waveform, and (interactive) the speed.
//
// Read-only: dots of dotSize with dotSpacing between them, wrapping when the row is too narrow.
// Interactive: each dot is a 28 x 44 radio target (never under 24 px wide) spread across the full
// width; every dot is 5 px, the chosen one in the accent, dots before it lit, dots after it
// waiting. Arrow keys move it.
import QtQuick
import QtQuick.Templates as T
import ".."

T.Control {
    id: meter

    property int count: 17
    property int lit: 0
    property real dotSize: Theme.dotSize
    property real dotSpacing: Theme.dotGap
    // Read-only: keep to one row; dots that don't fit are clipped on the right.
    property bool singleLine: false
    property color litColor: Theme.text
    property color offColor: Theme.wait
    // Optional per-dot colour, e.g. a waveform: function(index) -> color.
    property var colorAt: null

    property bool interactive: false
    property int current: -1
    property real cellWidth: 28
    // Narrower than count x cellWidth, the targets shrink to fit instead of overflowing.
    readonly property real fitCellWidth: count > 0 ? Math.min(cellWidth, width / count) : cellWidth
    property real cellHeight: Theme.controlHeight
    // Accessible name of each dot when interactive: function(index) -> string.
    property var labelAt: function(index) { return String(index + 1) }

    signal picked(int index)

    function dotColor(index) {
        if (interactive) {
            if (index === current)
                return Theme.accent
            return index < current ? litColor : offColor
        }
        if (colorAt)
            return colorAt(index)
        return index < lit ? litColor : offColor
    }

    function pick(index) {
        const next = Math.max(0, Math.min(count - 1, index))
        if (next !== current)
            picked(next)
    }

    implicitWidth: interactive ? count * cellWidth
                               : count * dotSize + Math.max(0, count - 1) * dotSpacing
    implicitHeight: interactive ? cellHeight : (singleLine ? dotSize : dotFlow.implicitHeight)
    focusPolicy: interactive ? Qt.StrongFocus : Qt.NoFocus
    activeFocusOnTab: interactive

    Accessible.role: interactive ? Accessible.Slider : Accessible.Indicator
    Accessible.name: interactive ? labelAt(current) : lit + " of " + count

    Keys.enabled: interactive
    Keys.onLeftPressed: pick(current - 1)
    Keys.onDownPressed: pick(current - 1)
    Keys.onRightPressed: pick(current + 1)
    Keys.onUpPressed: pick(current + 1)
    Keys.onPressed: (event) => {
        if (event.key === Qt.Key_Home) {
            pick(0)
            event.accepted = true
        } else if (event.key === Qt.Key_End) {
            pick(count - 1)
            event.accepted = true
        }
    }

    contentItem: Item {
        clip: meter.singleLine
        implicitWidth: meter.implicitWidth
        implicitHeight: meter.implicitHeight

        Flow {
            id: dotFlow
            visible: !meter.interactive
            width: parent.width
            spacing: meter.dotSpacing
            layoutDirection: Qt.LeftToRight

            Repeater {
                model: meter.interactive ? 0 : meter.count
                delegate: Rectangle {
                    required property int index
                    width: meter.dotSize
                    height: meter.dotSize
                    radius: width / 2
                    color: meter.dotColor(index)
                }
            }
        }

        Row {
            id: cells
            visible: meter.interactive
            anchors.fill: parent
            spacing: meter.count > 1 ? Math.max(0, (width - meter.count * meter.fitCellWidth) / (meter.count - 1)) : 0

            Repeater {
                model: meter.interactive ? meter.count : 0
                delegate: T.AbstractButton {
                    id: cell
                    required property int index
                    readonly property bool chosen: index === meter.current

                    width: meter.fitCellWidth
                    height: meter.cellHeight
                    focusPolicy: Qt.NoFocus
                    checkable: false
                    checked: chosen
                    Accessible.role: Accessible.RadioButton
                    Accessible.name: meter.labelAt(index)
                    Accessible.checkable: true
                    Accessible.checked: chosen
                    Accessible.onPressAction: meter.pick(index)
                    onClicked: {
                        meter.forceActiveFocus(Qt.MouseFocusReason)
                        meter.pick(index)
                    }

                    contentItem: Item {
                        Rectangle {
                            anchors.centerIn: parent
                            width: meter.dotSize
                            height: width
                            radius: width / 2
                            color: meter.dotColor(cell.index)
                        }
                    }
                }
            }
        }
    }

    FocusRing {
        target: meter
        gap: 2
        cornerRadius: 4
    }
}
