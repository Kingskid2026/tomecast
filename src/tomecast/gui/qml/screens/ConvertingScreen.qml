// [ 04 ] Converting: time left in Doto, the sentence grid, the live caption, Pause and Cancel.
// Wide: Converting.dc.html. Compact (corner window): Corner-converting.dc.html.
// While the voice model downloads, the download replaces the time-left readout.
import QtQuick
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    readonly property bool downloading: Backend.stage === "downloading"
    readonly property string voiceName: Backend.voiceLabel.split(" · ")[0]
    readonly property string coverDescription: "Cover of " + Backend.bookTitle + ", shown as dots"
    readonly property string pauseText: Backend.paused ? "Resume converting" : "Pause converting"

    function togglePause() {
        if (Backend.paused)
            Backend.resumeConversion()
        else
            Backend.pauseConversion()
    }

    objectName: "convertingScreen"
    wideDesignHeight: 780
    Accessible.role: Accessible.Pane
    Accessible.name: "Converting"

    wideView: ColumnLayout {
        spacing: Theme.sectionGap

        Header {
            Layout.fillWidth: true
            index: 4
            title: "Converting"

            MonoLabel {
                objectName: "convertingStatus"
                width: Math.min(implicitWidth, page.innerWidth)
                wrapMode: Text.Wrap
                text: Backend.hardwareLabel + " · VOICE " + page.voiceName + " · "
                      + Backend.speed.toFixed(2) + "×"
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
                objectName: "convertingCover"
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
                    spacing: 6

                    MonoLabel {
                        objectName: "chapterLabel"
                        Layout.fillWidth: true
                        text: Backend.chapterLabel
                        size: 12
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        objectName: "timeLeftReadout"
                        visible: !page.downloading
                        Layout.fillWidth: true
                        spacing: 16

                        DisplayNumber {
                            objectName: "timeLeft"
                            Layout.alignment: Qt.AlignBaseline
                            text: Backend.timeLeft
                            Accessible.name: Backend.timeLeft + " left"
                        }

                        MonoLabel {
                            Layout.alignment: Qt.AlignBaseline
                            Layout.fillWidth: true
                            text: "LEFT"
                            size: 12
                        }
                    }

                    Item {
                        visible: page.downloading
                        Layout.fillWidth: true
                        implicitHeight: Math.max(64, downloadText.implicitHeight)

                        MonoLabel {
                            id: downloadText
                            objectName: "downloadLabel"
                            anchors.verticalCenter: parent.verticalCenter
                            width: parent.width
                            text: Backend.downloadLabel
                            size: 13
                            color: Theme.text
                            wrapMode: Text.Wrap
                        }
                    }

                    MonoLabel {
                        objectName: "progressLabel"
                        Layout.fillWidth: true
                        text: Backend.progressLabel
                        size: 12
                        tracking: 0.1
                        wrapMode: Text.Wrap
                    }
                }

                SentenceGrid {
                    objectName: "sentenceGrid"
                    availableWidth: main.width
                    total: Backend.chunksTotal
                    done: Backend.chunksDone
                    complete: false
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.maximumWidth: 640
                    spacing: 8

                    MonoLabel {
                        text: "NOW SPEAKING"
                        tracking: 0.14
                        color: Theme.accent
                    }

                    Text {
                        objectName: "nowSpeaking"
                        Layout.fillWidth: true
                        text: Backend.nowSpeaking
                        color: Theme.text
                        font.family: Theme.mono
                        font.pixelSize: 13
                        font.letterSpacing: 13 * 0.05
                        font.capitalization: Font.AllUppercase
                        lineHeightMode: Text.FixedHeight
                        lineHeight: 22.1
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                }
            }
        }

        // Pushes the controls to the bottom of the window.
        Item { Layout.fillHeight: true }

        SplitRow {
            Layout.fillWidth: true
            gap: 16

            RowLayout {
                Layout.fillWidth: true
                Layout.maximumWidth: Number.POSITIVE_INFINITY
                spacing: 16

                RoundButton {
                    objectName: "pauseButton"
                    glyph: Backend.paused ? "play" : "pause"
                    text: page.pauseText
                    onClicked: page.togglePause()
                }

                Text {
                    objectName: "pauseNote"
                    Layout.fillWidth: true
                    text: Backend.paused ? "Paused. Everything voiced so far is kept."
                                         : "Pause any time. Everything voiced so far is kept."
                    color: Theme.muted
                    font.family: Theme.sans
                    font.pixelSize: 14
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }
            }

            PillButton {
                objectName: "cancelButton"
                text: "Cancel"
                onClicked: Backend.cancelConversion()
            }
        }
    }

    compactView: ColumnLayout {
        spacing: Theme.compactGap

        Header {
            Layout.fillWidth: true
            compact: true
            index: 4
            title: "Converting"

            // "4.7× REAL TIME", once it has been measured.
            MonoLabel {
                objectName: "convertingStatus"
                visible: Backend.speedLabel !== ""
                text: Backend.speedLabel
                size: 10
            }
        }

        // The cover beside the chapter, the time left and the progress.
        RowLayout {
            Layout.fillWidth: true
            spacing: 16

            DotImage {
                objectName: "convertingCover"
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
                    objectName: "chapterLabel"
                    Layout.fillWidth: true
                    text: Backend.chapterLabel
                    size: 10
                    tracking: 0.1
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 15
                    wrapMode: Text.Wrap
                }

                ColumnLayout {
                    objectName: "timeLeftReadout"
                    visible: !page.downloading
                    Layout.fillWidth: true
                    spacing: 6

                    DisplayNumber {
                        objectName: "timeLeft"
                        Layout.topMargin: 6
                        size: 38
                        text: Backend.timeLeft
                        Accessible.name: Backend.timeLeft + " left"
                    }

                    MonoLabel {
                        text: "LEFT"
                        size: 10
                    }
                }

                MonoLabel {
                    objectName: "downloadLabel"
                    visible: page.downloading
                    Layout.fillWidth: true
                    Layout.topMargin: 6
                    text: Backend.downloadLabel
                    size: 10
                    tracking: 0.1
                    color: Theme.text
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 15
                    wrapMode: Text.Wrap
                }

                MonoLabel {
                    objectName: "progressLabel"
                    Layout.fillWidth: true
                    Layout.topMargin: 8
                    text: Backend.progressLabel
                    size: 10
                    tracking: 0.08
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 15
                    wrapMode: Text.Wrap
                }
            }
        }

        SentenceGrid {
            objectName: "sentenceGrid"
            compact: true
            availableWidth: page.innerWidth
            total: Backend.chunksTotal
            done: Backend.chunksDone
            complete: false
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6

            MonoLabel {
                text: "NOW SPEAKING"
                size: 10
                tracking: 0.14
                color: Theme.accent
            }

            Text {
                objectName: "nowSpeaking"
                Layout.fillWidth: true
                text: Backend.nowSpeaking
                color: Theme.text
                font.family: Theme.mono
                font.pixelSize: 11
                font.letterSpacing: 11 * 0.04
                font.capitalization: Font.AllUppercase
                lineHeightMode: Text.FixedHeight
                lineHeight: 17.6
                wrapMode: Text.Wrap
                maximumLineCount: 4
                elide: Text.ElideRight
                textFormat: Text.PlainText
            }
        }

        // Pushes the controls to the bottom of the window.
        Item { Layout.fillHeight: true }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            RoundButton {
                objectName: "pauseButton"
                compact: true
                glyph: Backend.paused ? "play" : "pause"
                text: page.pauseText
                onClicked: page.togglePause()
            }

            Item { Layout.fillWidth: true }

            PillButton {
                objectName: "cancelButton"
                compact: true
                implicitHeight: 36
                leftPadding: 16
                rightPadding: 16
                text: "Cancel"
                onClicked: Backend.cancelConversion()
            }
        }
    }
}
