// Remembers the window's size and position between runs (QSettings, "window" group, through
// QtCore's Settings type; Backend has no slot for it). It restores on load, and saves a moment
// after the window stops moving. Only a normal (not maximised or full-screen) window is saved,
// and a saved position is used only when it still lands on a screen.
import QtQuick
import QtCore

Item {
    id: memory

    required property Window target

    visible: false

    Settings {
        id: store
        category: "window"
        property int width: 0
        property int height: 0
        property int x: -1
        property int y: -1
    }

    Timer {
        id: saveSoon
        interval: 400
        onTriggered: {
            const w = memory.target
            if (w.visibility !== Window.Windowed)
                return
            store.width = w.width
            store.height = w.height
            store.x = w.x
            store.y = w.y
        }
    }

    Connections {
        target: memory.target
        function onWidthChanged() { saveSoon.restart() }
        function onHeightChanged() { saveSoon.restart() }
        function onXChanged() { saveSoon.restart() }
        function onYChanged() { saveSoon.restart() }
    }

    Component.onCompleted: {
        const w = memory.target
        if (store.width >= w.minimumWidth && store.height >= w.minimumHeight) {
            w.width = store.width
            w.height = store.height
        }
        // A position only means something where windows can be placed (not on Wayland), and
        // only if it is still on a screen.
        if (store.x >= 0 && store.y >= 0
                && store.x + 100 < Screen.desktopAvailableWidth
                && store.y + 100 < Screen.desktopAvailableHeight) {
            w.x = store.x
            w.y = store.y
        }
    }
}
