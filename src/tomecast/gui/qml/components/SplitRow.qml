// Two items spread to the left and right edges, stacking when they don't fit side by side
// (flex-wrap with space-between). Give the first item Layout.fillWidth: true (a nested layout
// also needs Layout.maximumWidth: Number.POSITIVE_INFINITY, or it can't grow past its content).
import QtQuick
import QtQuick.Layouts

GridLayout {
    id: row

    property real gap: 12
    readonly property bool stacked: {
        const items = []
        for (let i = 0; i < children.length; ++i) {
            if (children[i].visible)
                items.push(children[i])
        }
        if (items.length < 2)
            return false
        return items[0].implicitWidth + gap + items[1].implicitWidth > width
    }

    columns: stacked ? 1 : 2
    columnSpacing: gap
    rowSpacing: gap
}
