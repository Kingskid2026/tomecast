// The frame every screen sits in: page padding, and either the screen's wide view (the
// 1200 px mockups) or its compact view (the 400 x 600 corner-window mockups), whichever fits.
// Only one view exists at a time. The page scrolls when the window is shorter than the view
// needs, and keyboard focus scrolls into view.
import QtQuick
import QtQuick.Controls.Basic as Controls
import QtQuick.Layouts
import ".."

Flickable {
    id: page

    property Component wideView
    property Component compactView
    // Anything else (a FileDialog, say) can be declared inside the page.
    default property alias content: column.data
    readonly property Item view: viewLoader.item

    // The corner-window layout, below Theme.compactBelow.
    readonly property bool compact: width < Theme.compactBelow
    property real topPadding: compact ? Theme.compactPadV : Theme.pageTop
    property real bottomPadding: compact ? Theme.compactPadV : Theme.pageBottom
    property real sidePadding: compact ? Theme.compactPadH : Theme.pageSide
    // The height the approved mockup was drawn at (600 for the corner window); screenshots use
    // max(designHeight, naturalHeight).
    property int wideDesignHeight: 720
    readonly property int designHeight: compact ? 600 : wideDesignHeight
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
        spacing: 0

        Loader {
            id: viewLoader
            Layout.fillWidth: true
            Layout.fillHeight: true
            sourceComponent: page.compact && page.compactView ? page.compactView : page.wideView
        }
    }
}
