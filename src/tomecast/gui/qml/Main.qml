// Tomecast's window: shows the screen named by Backend.screen (docs/gui.md).
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import TomecastNative
import "."
import "components"
import "screens"

ApplicationWindow {
    id: window

    // True while a file is dragged over the window (the drop screen's title turns the accent).
    readonly property bool bookDragActive: dropArea.containsDrag

    objectName: "mainWindow"
    width: 1200
    height: 780
    // Often small, parked in a corner: every screen reflows down to 400 x 560.
    minimumWidth: 400
    minimumHeight: 560
    visible: true
    title: Backend.bookLoaded && Backend.bookTitle !== "" ? Backend.bookTitle + " · Tomecast" : "Tomecast"
    color: Theme.bg
    font.family: Theme.sans
    font.pixelSize: 15

    // Size and position come back as they were. Only the real app (which names its
    // organization) does this, so tests and screenshots leave the user's settings alone.
    Loader {
        active: Qt.application.organization !== ""
        sourceComponent: WindowMemory { target: window }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        ErrorBanner {
            objectName: "errorBanner"
            Layout.fillWidth: true
            visible: Backend.errorMessage !== ""
            message: Backend.errorMessage
            onDismissed: Backend.dismissError()
        }

        Item {
            id: stack
            objectName: "screenStack"
            Layout.fillWidth: true
            Layout.fillHeight: true

            ScreenSlot { name: "drop"; sourceComponent: dropScreen }
            ScreenSlot { name: "chapters"; sourceComponent: chaptersScreen }
            ScreenSlot { name: "voice"; sourceComponent: voiceScreen }
            ScreenSlot { name: "converting"; sourceComponent: convertingScreen }
            ScreenSlot { name: "done"; sourceComponent: doneScreen }
        }
    }

    DropArea {
        id: dropArea
        objectName: "bookDropArea"
        anchors.fill: parent
        enabled: Backend.screen !== "converting" && !Backend.loading
        onEntered: (drag) => { drag.accepted = drag.hasUrls }
        onDropped: (drop) => {
            if (drop.hasUrls && drop.urls.length > 0) {
                Backend.openBook(drop.urls[0])
                drop.acceptProposedAction()
            }
        }
    }

    // One screen's place in the stack: loaded while it's shown or fading, cross-fading in
    // 200 ms (instantly with reduced motion).
    component ScreenSlot: Loader {
        property string name
        readonly property bool current: Backend.screen === name

        objectName: name + "Slot"
        anchors.fill: parent
        opacity: current ? 1 : 0
        // Loaded at once when it becomes current; unloaded once it has faded out.
        active: current || opacity > 0
        visible: active
        enabled: current
        z: current ? 1 : 0
        focus: current

        Behavior on opacity {
            enabled: Theme.motion
            NumberAnimation { duration: Theme.fadeMs; easing.type: Easing.InOutQuad }
        }
    }

    Component { id: dropScreen; DropScreen { dragActive: window.bookDragActive } }
    Component { id: chaptersScreen; ChaptersScreen {} }
    Component { id: voiceScreen; VoiceScreen {} }
    Component { id: convertingScreen; ConvertingScreen {} }
    Component { id: doneScreen; DoneScreen {} }
}
