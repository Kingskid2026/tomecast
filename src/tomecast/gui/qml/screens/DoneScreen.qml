// [ 05 ] Done: the full grid lit, the length, the file and Open folder.
// Wide: Done.dc.html. Compact (corner window): Corner-done.dc.html.
import QtQuick
import QtCore
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    readonly property string homePath: {
        const home = StandardPaths.writableLocation(StandardPaths.HomeLocation).toString()
        return home.indexOf("file://") === 0 ? decodeURIComponent(home.substring(7)) : home
    }
    // The saved file, with the home folder written as "~".
    readonly property string shortPath: homePath !== "" && Backend.outputPath.indexOf(homePath + "/") === 0
                                        ? "~" + Backend.outputPath.substring(homePath.length)
                                        : Backend.outputPath
    readonly property string checkedCount: grouped(Backend.chunksTotal) + " OF " + grouped(Backend.chunksTotal)
    readonly property string coverDescription: "Cover of " + Backend.bookTitle + ", shown as dots"

    function grouped(n) {
        return Number(n).toLocaleString(Qt.locale("en_US"), "f", 0)
    }

    objectName: "doneScreen"
    wideDesignHeight: 720
    Accessible.role: Accessible.Pane
    Accessible.name: "Done"

    // Problems that didn't stop the book (a damaged cover left out, say).
    component Warnings: ColumnLayout {
        id: list
        property bool compact: false

        objectName: "doneWarnings"
        visible: Backend.warnings.length > 0
        spacing: 6

        Repeater {
            model: Backend.warnings
            delegate: MonoLabel {
                required property string modelData
                Layout.fillWidth: true
                text: modelData.toUpperCase()
                size: list.compact ? 10 : 11
                wrapMode: Text.Wrap
            }
        }
    }

    wideView: ColumnLayout {
        spacing: Theme.sectionGap

        Header {
            Layout.fillWidth: true
            index: 5
            title: "Done"

            MonoLabel {
                objectName: "checkedLabel"
                width: Math.min(implicitWidth, page.innerWidth)
                wrapMode: Text.Wrap
                text: "EVERY SENTENCE CHECKED · " + page.checkedCount
            }
        }

        GridLayout {
            id: body
            readonly property bool sideBySide: page.innerWidth >= 200 + Theme.columnGap + 560

            Layout.fillWidth: true
            columns: sideBySide ? 2 : 1
            columnSpacing: Theme.columnGap
            rowSpacing: Theme.columnGap

            DotImage {
                objectName: "doneCover"
                Layout.alignment: Qt.AlignTop
                visible: Backend.coverSource !== ""
                source: Backend.coverSource
                displayWidth: 200
                placeholderHeight: 276
                description: page.coverDescription
            }

            ColumnLayout {
                id: main
                Layout.alignment: Qt.AlignTop
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: 22

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 8

                    MonoLabel {
                        objectName: "finishedLabel"
                        Layout.fillWidth: true
                        text: "FINISHED · " + Backend.bookTitle.toUpperCase()
                        size: 12
                        wrapMode: Text.Wrap
                    }

                    DisplayNumber {
                        objectName: "audioLength"
                        text: Backend.audioLength
                        Accessible.name: Backend.audioLength + " " + Backend.doneSummary.toLowerCase()
                    }

                    MonoLabel {
                        objectName: "doneSummary"
                        Layout.fillWidth: true
                        text: Backend.doneSummary
                        size: 12
                        tracking: 0.1
                        wrapMode: Text.Wrap
                    }
                }

                SentenceGrid {
                    objectName: "completeGrid"
                    availableWidth: main.width
                    total: Backend.chunksTotal
                    done: Backend.chunksTotal
                    complete: true
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 6

                    MonoLabel {
                        text: "SAVED AS"
                        tracking: 0.14
                    }

                    Text {
                        objectName: "outputPath"
                        Layout.fillWidth: true
                        text: page.shortPath
                        color: Theme.text
                        font.family: Theme.mono
                        font.pixelSize: 15
                        wrapMode: Text.WrapAnywhere
                        textFormat: Text.PlainText
                    }
                }

                Warnings {
                    Layout.fillWidth: true
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 10

                    PillButton {
                        objectName: "openFolderButton"
                        variant: "filled"
                        text: "Open folder"
                        onClicked: Backend.openOutputFolder()
                    }

                    PillButton {
                        objectName: "convertAnotherButton"
                        text: "Convert another book"
                        onClicked: Backend.convertAnother()
                    }
                }
            }
        }

        // Spare height stays at the bottom.
        Item { Layout.fillHeight: true }
    }

    compactView: ColumnLayout {
        spacing: Theme.compactGap

        Header {
            Layout.fillWidth: true
            compact: true
            index: 5
            title: "Done"

            MonoLabel {
                objectName: "checkedLabel"
                text: page.checkedCount + " CHECKED"
                size: 10
            }
        }

        // The cover beside what was made and how long it is.
        RowLayout {
            Layout.fillWidth: true
            spacing: 16

            DotImage {
                objectName: "doneCover"
                Layout.alignment: Qt.AlignTop
                visible: Backend.coverSource !== ""
                source: Backend.coverSource
                displayWidth: 120
                placeholderHeight: 166
                description: page.coverDescription
            }

            ColumnLayout {
                Layout.alignment: Qt.AlignTop
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.topMargin: 2
                spacing: 6

                MonoLabel {
                    objectName: "finishedLabel"
                    Layout.fillWidth: true
                    text: "FINISHED · " + Backend.bookTitle.toUpperCase()
                    size: 10
                    tracking: 0.1
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 15
                    wrapMode: Text.Wrap
                }

                DisplayNumber {
                    objectName: "audioLength"
                    Layout.topMargin: 6
                    size: 38
                    text: Backend.audioLength
                    Accessible.name: Backend.audioLength + " " + Backend.doneSummary.toLowerCase()
                }

                MonoLabel {
                    objectName: "doneSummary"
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    text: Backend.doneSummary
                    size: 10
                    tracking: 0.08
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 15
                    wrapMode: Text.Wrap
                }
            }
        }

        SentenceGrid {
            objectName: "completeGrid"
            compact: true
            availableWidth: page.innerWidth
            total: Backend.chunksTotal
            done: Backend.chunksTotal
            complete: true
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6

            MonoLabel {
                text: "SAVED AS"
                size: 10
                tracking: 0.14
            }

            Text {
                objectName: "outputPath"
                Layout.fillWidth: true
                text: page.shortPath
                color: Theme.text
                font.family: Theme.mono
                font.pixelSize: 12
                lineHeightMode: Text.FixedHeight
                lineHeight: 18
                wrapMode: Text.WrapAnywhere
                textFormat: Text.PlainText
            }
        }

        Warnings {
            Layout.fillWidth: true
            compact: true
        }

        // Pushes the buttons to the bottom of the window.
        Item { Layout.fillHeight: true }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 8

            PillButton {
                objectName: "openFolderButton"
                Layout.fillWidth: true
                compact: true
                variant: "filled"
                text: "Open folder"
                onClicked: Backend.openOutputFolder()
            }

            PillButton {
                objectName: "convertAnotherButton"
                Layout.fillWidth: true
                compact: true
                text: "Convert another book"
                onClicked: Backend.convertAnother()
            }
        }
    }
}
