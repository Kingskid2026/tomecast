// [ 01 ] Drop a book: the Cantoria in dots, "DROP A BOOK HERE", and Choose a book.
// Wide: Main.dc.html. Compact (corner window): Corner-drop.dc.html.
// While a file is dragged over the window, "DROP A BOOK HERE" turns the accent colour.
import QtQuick
import QtQuick.Dialogs
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    // A file is being dragged over the window.
    property bool dragActive: false
    readonly property string titleText: Backend.loading ? "READING THE BOOK…" : "DROP A BOOK HERE"
    readonly property color titleColor: dragActive ? Theme.accent : Theme.text
    readonly property string artDescription: "Luca della Robbia's Cantoria, marble children singing, shown as dots"

    function chooseBook() {
        // A file:// URL from the backend (a plain path also works).
        const folder = Backend.chooseBookFolder()
        if (folder !== "")
            fileDialog.currentFolder = folder.indexOf("file:") === 0 ? folder : "file://" + folder
        fileDialog.open()
    }

    objectName: "dropScreen"
    wideDesignHeight: 720
    bottomPadding: compact ? Theme.compactPadV : Theme.pageTop
    Accessible.role: Accessible.Pane
    Accessible.name: "Drop a book"

    wideView: ColumnLayout {
        spacing: 24

        Header {
            Layout.fillWidth: true

            MonoLabel {
                objectName: "hardwareLabel"
                text: Backend.hardwareLabel
            }
        }

        // The field: 40 px padding around a content box at least 520 px tall.
        Item {
            id: field
            objectName: "dropZone"
            readonly property real room: width - 2 * 40

            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: Math.max(520, art.implicitHeight) + 2 * 40

            GridLayout {
                id: art
                // Art beside the words, or above them (centred) when they don't fit side by side.
                readonly property bool sideBySide: figure.implicitWidth + 56 + 420 <= field.room

                anchors.centerIn: parent
                width: sideBySide ? implicitWidth : Math.min(field.room, 420)
                columns: sideBySide ? 2 : 1
                columnSpacing: 56
                rowSpacing: 40

                ColumnLayout {
                    id: figure
                    Layout.alignment: art.sideBySide ? Qt.AlignVCenter : Qt.AlignHCenter
                    spacing: 10

                    DotImage {
                        objectName: "artImage"
                        source: Backend.artSource
                        displayWidth: 240
                        placeholderHeight: 358
                        description: page.artDescription
                    }

                    MonoLabel {
                        objectName: "artCredit"
                        Layout.maximumWidth: field.room
                        text: Backend.artCredit
                        size: 10
                        wrapMode: Text.Wrap
                    }
                }

                ColumnLayout {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.maximumWidth: 420
                    Layout.preferredWidth: Math.min(420, field.room)
                    spacing: 18

                    MonoLabel {
                        objectName: "dropTitle"
                        Layout.fillWidth: true
                        text: page.titleText
                        size: 15
                        font.weight: Font.Medium
                        tracking: 0.18
                        color: page.titleColor
                        wrapMode: Text.Wrap

                        Behavior on color {
                            enabled: Theme.motion
                            ColorAnimation { duration: Theme.quickMs }
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: "Tomecast turns it into an audiobook with chapters, right on this computer."
                        color: Theme.muted
                        font.family: Theme.sans
                        font.pixelSize: 15
                        lineHeightMode: Text.FixedHeight
                        lineHeight: 24
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }

                    MonoLabel {
                        objectName: "formatsLabel"
                        Layout.fillWidth: true
                        text: Backend.formatsLabel
                        tracking: 0.14
                        wrapMode: Text.Wrap
                    }

                    PillButton {
                        objectName: "chooseBookButton"
                        variant: "strong"
                        tracking: 0.14
                        text: "Choose a book"
                        enabled: !Backend.loading
                        onClicked: page.chooseBook()
                    }
                }
            }
        }

        SplitRow {
            Layout.fillWidth: true

            MonoLabel {
                objectName: "noBookLabel"
                Layout.fillWidth: true
                text: "[ 00 ] NO BOOK YET"
            }

            MonoLabel {
                Layout.maximumWidth: page.innerWidth
                text: "KOKORO 82M · AUDIO.CPP 0.9.4 · EVERYTHING STAYS ON THIS COMPUTER"
                wrapMode: Text.Wrap
            }
        }
    }

    compactView: ColumnLayout {
        spacing: Theme.compactGap

        Header {
            Layout.fillWidth: true
            compact: true

            MonoLabel {
                objectName: "hardwareLabel"
                // The corner window drops the "ABOUT", as in its mockup.
                text: Backend.hardwareLabel.replace("ABOUT ", "")
                size: 10
            }
        }

        // The field: 18 px padding around one centred column.
        Item {
            id: field
            objectName: "dropZone"
            readonly property real room: width - 2 * 18

            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: stack.implicitHeight + 2 * 18

            ColumnLayout {
                id: stack
                anchors.centerIn: parent
                width: Math.min(implicitWidth, field.room)
                spacing: 14

                ColumnLayout {
                    Layout.alignment: Qt.AlignHCenter
                    spacing: 8

                    DotImage {
                        objectName: "artImage"
                        Layout.alignment: Qt.AlignHCenter
                        source: Backend.artSource
                        displayWidth: 160
                        placeholderHeight: 239
                        description: page.artDescription
                    }

                    MonoLabel {
                        objectName: "artCredit"
                        Layout.alignment: Qt.AlignHCenter
                        Layout.maximumWidth: field.room
                        horizontalAlignment: Text.AlignHCenter
                        text: Backend.artCredit
                        size: 9
                        wrapMode: Text.Wrap
                    }
                }

                MonoLabel {
                    objectName: "dropTitle"
                    Layout.alignment: Qt.AlignHCenter
                    Layout.maximumWidth: field.room
                    horizontalAlignment: Text.AlignHCenter
                    text: page.titleText
                    size: 13
                    font.weight: Font.Medium
                    tracking: 0.18
                    color: page.titleColor
                    wrapMode: Text.Wrap

                    Behavior on color {
                        enabled: Theme.motion
                        ColorAnimation { duration: Theme.quickMs }
                    }
                }

                MonoLabel {
                    objectName: "formatsLabel"
                    Layout.alignment: Qt.AlignHCenter
                    Layout.maximumWidth: field.room
                    horizontalAlignment: Text.AlignHCenter
                    text: Backend.formatsLabel
                    size: 10
                    wrapMode: Text.Wrap
                }

                PillButton {
                    objectName: "chooseBookButton"
                    Layout.alignment: Qt.AlignHCenter
                    variant: "strong"
                    compact: true
                    tracking: 0.14
                    text: "Choose a book"
                    enabled: !Backend.loading
                    onClicked: page.chooseBook()
                }
            }
        }

        SplitRow {
            Layout.fillWidth: true
            gap: 8

            MonoLabel {
                objectName: "noBookLabel"
                Layout.fillWidth: true
                text: "[ 00 ] NO BOOK YET"
                size: 10
            }

            MonoLabel {
                text: "STAYS ON THIS COMPUTER"
                size: 10
            }
        }
    }

    FileDialog {
        id: fileDialog
        objectName: "bookFileDialog"
        title: "Choose a book"
        fileMode: FileDialog.OpenFile
        nameFilters: [
            "Books (*.epub *.pdf *.docx *.fb2 *.html *.htm *.txt)",
            "All files (*)"
        ]
        onAccepted: Backend.openBook(selectedFile)
    }
}
