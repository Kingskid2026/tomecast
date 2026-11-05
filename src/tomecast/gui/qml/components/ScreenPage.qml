// The frame every screen sits in: page padding and the gap between sections. There is one
// design; in a window 680 px wide or less (`narrow`) the padding, the gaps and the big numerals
// shrink as in the mockups' narrow-window rules, and the screen's own rows reflow (they
// stack where the mockup's flex rows wrap). The page scrolls vertically when the window is
// shorter than the content needs, never sideways, and keyboard focus scrolls into view.
import QtQuick
import QtQuick.Controls.Basic as Controls
import QtQuick.Layouts
import ".."

Flickable {
    id: page

    // The screen's content goes into one column (the section gap apart). Anything else (a
    // FileDialog, say) can be declared here too.
    default property alias content: column.data

    // 680 px or less: the mockups' narrow rules.
    readonly property bool narrow: width <= Theme.narrowMaxWidth
    property real topPadding: narrow ? Theme.narrowPadTop : Theme.pageTop
    property real bottomPadding: narrow ? Theme.narrowPadBottom : Theme.pageBottom
    property real sidePadding: narrow ? Theme.narrowPadSide : Theme.pageSide
    property real sectionGap: narrow ? Theme.narrowGap : Theme.sectionGap
    // Doto numerals (time left, audio length): 67 px at every width, so its dots are 5 px.
    readonly property int bigNumberSize: Theme.bigNumber
    // The height the mockup was drawn at; screenshots use max(designHeight, naturalHeight).
    property int designHeight: 720
    readonly property real naturalHeight: Math.ceil(column.implicitHeight + topPadding + bottomPadding)
    readonly property real innerWidth: Math.max(0, width - 2 * sidePadding)

    function ensureVisible(item) {
        if (!item)
            return
        let node = item
        while (node && node !== page.contentItem)
            node = node.parent
        if (!node)
            return
        const top = item.mapToItem(page.contentItem, 0, 0).y - 12
        const bottom = top + item.height + 24
        if (top < contentY)
            contentY = Math.max(0, top)
        else if (bottom > contentY + height)
            contentY = Math.min(contentHeight - height, bottom - height)
    }

    // A page takes the room it is given; its content never asks for more (a Flickable's
    // implicit size follows its content, which would pin the window's minimum width).
    implicitWidth: 0
    implicitHeight: 0
    contentWidth: width
    contentHeight: Math.max(height, naturalHeight)
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    flickableDirection: Flickable.VerticalFlick

    Controls.ScrollBar.vertical: Controls.ScrollBar {
        policy: page.contentHeight > page.height + 1 ? Controls.ScrollBar.AsNeeded : Controls.ScrollBar.AlwaysOff
        contentItem: Rectangle {
            implicitWidth: 4
            radius: 2
            color: Theme.muted
            opacity: parent.active ? 0.8 : 0.35
        }
    }

    Connections {
        target: page.Window.window
        function onActiveFocusItemChanged() {
            if (page.visible && page.Window.window)
                page.ensureVisible(page.Window.window.activeFocusItem)
        }
    }

    ColumnLayout {
        id: column
        x: page.sidePadding
        y: page.topPadding
        width: page.innerWidth
        height: page.contentHeight - page.topPadding - page.bottomPadding
        spacing: page.sectionGap
    }
}
