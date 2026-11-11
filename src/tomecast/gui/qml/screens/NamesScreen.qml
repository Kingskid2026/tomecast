// Names: the words the voice has to guess, with a "say it as" fix and a player for each, and a
// side panel to try any word and see every fix (Names-B-own-step.dc.html). Its own step after
// Voice, only for books where the name finder found something. In a narrow window the count
// column goes and the side panel moves under the table.
import QtQuick
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    // The table shows the 8 most frequent names until "Show all" is pressed.
    readonly property int firstRows: 8
    property bool showAll: false
    readonly property int shownCount: showAll ? Backend.names.length
                                              : Math.min(firstRows, Backend.names.length)
    // A fix row whose buttons had focus before the list was rebuilt: {word, kind, index,
    // keyboard}. The rebuilt row takes focus back.
    property var refocus: null
    // The waveform (decorative), as on the voice screen.
    readonly property var levels: [3, 5, 4, 7, 6, 8, 5, 3, 6, 8, 7, 4, 5, 6, 3, 2, 5, 7, 8, 6,
                                   4, 6, 5, 3, 4, 6, 7, 5, 3, 2, 4, 6, 5, 4, 3, 5, 4, 2, 3, 2]

    function escaped(text) {
        return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }

    // A fix row's pill or Remove was clicked. Its buttons don't take focus on the press: a
    // name being typed is saved when focus leaves its field, saving rebuilds the list, and a
    // press and release that land on different rows make no click. So focus moves now, which
    // saves the name first, and then the fix that was clicked changes. Done here, not in the
    // row, since the save may already have replaced the row.
    function fixClicked(button, kind, scope, word) {
        const keyboard = button.visualFocus
        button.forceActiveFocus(Qt.MouseFocusReason)
        const now = Backend.fixes.findIndex(fix => fix.word === word && fix.scope === scope)
        if (now < 0)
            return  // the save removed this fix (its name's field was emptied)
        refocus = {word: word, kind: kind, index: now, keyboard: keyboard}
        if (kind === "scope")
            Backend.toggleFixScope(scope, word)
        else
            Backend.removeFix(scope, word)
        refocus = null  // the rebuilt row took it; if nothing changed, no later rebuild should
    }

    objectName: "namesScreen"
    designHeight: 960
    Accessible.role: Accessible.Pane
    Accessible.name: "Names"

    // One name: the word, how often the book uses it, how to say it, and a player.
    component NameRow: Item {
        id: row

        required property int index
        required property real nameWidth
        required property real sayWidth
        required property real gap
        readonly property var name: Backend.names[index] || ({})
        readonly property string word: name.word || ""
        readonly property string saved: Backend.nameFixes[word] || ""
        readonly property bool playing: word !== "" && Backend.playingName === word

        objectName: "nameRow" + index
        // 60 px plus the 1 px rule under it.
        implicitHeight: 61

        // A fix changed elsewhere (REMOVE, say) shows here, unless it's being typed.
        onSavedChanged: if (!field.activeFocus) field.text = saved

        Text {
            objectName: "nameWord" + row.index
            width: row.nameWidth
            height: 60
            verticalAlignment: Text.AlignVCenter
            text: row.word
            color: Theme.text
            font.family: Theme.sans
            font.pixelSize: 16
            elide: Text.ElideRight
            textFormat: Text.PlainText
        }

        Row {
            objectName: "nameCount" + row.index
            visible: !page.narrow
            x: row.nameWidth + row.gap
            height: 60
            spacing: 10

            MonoLabel {
                width: 34
                anchors.verticalCenter: parent.verticalCenter
                text: row.name.countLabel || ""
                size: 11
                tracking: 0
                Accessible.name: (row.name.count || 0) + " times in the book"
            }

            DotMeter {
                objectName: "nameMeter" + row.index
                anchors.verticalCenter: parent.verticalCenter
                count: 11
                lit: row.name.meterLit || 0
                dotSize: Theme.dotSize
                dotSpacing: Theme.dotGap
                Accessible.ignored: true
            }
        }

        LineField {
            id: field
            objectName: "nameField" + row.index
            x: page.narrow ? row.nameWidth + row.gap : row.nameWidth + 150 + 2 * row.gap
            y: 8
            width: row.sayWidth
            placeholderText: "as written"
            lineColor: text.trim() !== "" ? Theme.text : Theme.line
            Accessible.name: "Say " + row.word + " as"
            Component.onCompleted: text = row.saved
            // Enter, Tab or a click elsewhere: save it (an empty field removes the fix), then
            // show what was saved (a fix the list can't hold isn't). Not when the screen is
            // going away (another book was dropped): the text belonged to the old book.
            onEditingFinished: {
                if (row.enabled)
                    Backend.setNameFix(row.word, text)
                text = row.saved
            }
        }

        RoundButton {
            objectName: "namePlay" + row.index
            x: row.width - width
            y: 8
            size: Theme.controlHeight
            glyphSize: 12
            ink: row.playing ? Theme.accent : Theme.text
            glyph: row.playing ? "pause" : "play"
            enabled: Backend.previewAvailable
            text: row.playing ? "Stop " + row.word : "Hear " + row.word
            onClicked: Backend.playName(row.word)
        }

        Rectangle {
            y: 60
            width: row.width
            height: 1
            color: Theme.line
        }
    }

    // One saved fix: "Word → say-it", which list it's in (a switch) and Remove. The buttons go
    // under the text when the panel is too narrow for both, like the mockup's wrapping row.
    component FixRow: Item {
        id: fixRow

        required property var modelData
        required property int index
        readonly property string word: modelData.word
        readonly property string scope: modelData.scope
        readonly property real buttonsWidth: scopeButton.implicitWidth + 10 + removeButton.implicitWidth
        readonly property bool wraps: width < 160 + 10 + buttonsWidth
        readonly property real textWidth: wraps ? width : width - 10 - buttonsWidth
        readonly property real contentHeight: wraps ? label.implicitHeight + 10 + 36
                                                    : Math.max(label.implicitHeight, 36)
        // 56 px (or what the text needs) plus the 1 px rule under it.
        readonly property real boxHeight: Math.max(56, contentHeight)

        objectName: "fixRow" + index
        implicitHeight: boxHeight + 1

        Component.onCompleted: {
            const wanted = page.refocus
            if (!wanted)
                return
            const reason = wanted.keyboard ? Qt.TabFocusReason : Qt.MouseFocusReason
            if (wanted.kind === "scope" && wanted.word === word) {
                page.refocus = null
                scopeButton.forceActiveFocus(reason)
            } else if (wanted.kind === "remove"
                       && index === Math.min(wanted.index, Backend.fixes.length - 1)) {
                page.refocus = null
                removeButton.forceActiveFocus(reason)
            }
        }

        Text {
            id: label
            objectName: "fixText" + fixRow.index
            y: (fixRow.boxHeight - fixRow.contentHeight) / 2
               + (fixRow.wraps ? 0 : (fixRow.contentHeight - implicitHeight) / 2)
            width: fixRow.textWidth
            // Rich text: StyledText can't change the font, and the respelling is in Plex Mono.
            text: page.escaped(fixRow.word) + " <span style=\"color: " + Theme.muted + "\">→</span> "
                  + "<span style=\"font-family: '" + Theme.mono + "'\">"
                  + page.escaped(fixRow.modelData.say) + "</span>"
            textFormat: Text.RichText
            color: Theme.text
            font.family: Theme.sans
            font.pixelSize: 15
            wrapMode: Text.Wrap
            Accessible.role: Accessible.StaticText
            Accessible.name: fixRow.word + ", said as " + fixRow.modelData.say
        }

        TagButton {
            id: scopeButton
            objectName: "fixScope" + fixRow.index
            x: fixRow.wraps ? 0 : fixRow.width - fixRow.buttonsWidth
            y: fixRow.wraps ? label.y + label.implicitHeight + 10 : (fixRow.boxHeight - height) / 2
            text: fixRow.scope === "all" ? "Every book" : "This book"
            Accessible.name: fixRow.scope === "all" ? "Use the " + fixRow.word + " fix for this book only"
                                                    : "Use the " + fixRow.word + " fix for every book"
            focusPolicy: Qt.TabFocus  // focus moves on the click (see page.fixClicked)
            onClicked: page.fixClicked(scopeButton, "scope", fixRow.scope, fixRow.word)
        }

        TagButton {
            id: removeButton
            objectName: "fixRemove" + fixRow.index
            plain: true
            x: scopeButton.x + scopeButton.width + 10
            y: scopeButton.y
            text: "Remove"
            Accessible.name: "Remove the fix for " + fixRow.word
            focusPolicy: Qt.TabFocus  // focus moves on the click (see page.fixClicked)
            onClicked: page.fixClicked(removeButton, "remove", fixRow.scope, fixRow.word)
        }

        Rectangle {
            y: fixRow.boxHeight
            width: fixRow.width
            height: 1
            color: Theme.line
        }
    }

    Header {
        Layout.fillWidth: true
        title: "Names"

        ColumnLayout {
            spacing: 10

            // What Start will download, if anything (the first time only), as on Voice.
            MonoLabel {
                objectName: "namesDownloadNote"
                Layout.alignment: Qt.AlignRight
                Layout.fillWidth: true
                Layout.maximumWidth: page.innerWidth
                visible: Backend.downloadNote !== ""
                text: Backend.downloadNote
                size: 10
                tracking: 0.12
                horizontalAlignment: Text.AlignRight
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.alignment: Qt.AlignRight
                spacing: 10

                PillButton {
                    objectName: "namesBackButton"
                    text: "Back"
                    onClicked: Backend.goTo("voice")
                }

                PillButton {
                    objectName: "namesStartButton"
                    variant: "filled"
                    text: "Start converting"
                    enabled: !Backend.loading
                    onClicked: Backend.startConversion()
                }
            }
        }
    }

    GridLayout {
        id: body
        // The table (at least 520 px) and the side panel (360 px) side by side, 48 px apart.
        readonly property bool sideBySide: page.innerWidth >= 520 + 48 + 360

        Layout.fillWidth: true
        columns: sideBySide ? 2 : 1
        columnSpacing: 48
        rowSpacing: 48

        // The names table.
        ColumnLayout {
            id: table

            // The mockup's grid: name, count (150), say it as, play (44); narrow drops the count.
            readonly property real gap: page.narrow ? 10 : 16
            readonly property real fixedWidth: page.narrow ? 44 + 2 * gap : 150 + 44 + 3 * gap
            readonly property real free: Math.max(0, width - fixedWidth)
            readonly property real nameWidth: page.narrow ? free / 2.4 : Math.min(free, Math.max(110, free / 2.5))
            readonly property real sayWidth: free - nameWidth

            objectName: "namesTable"
            Layout.alignment: Qt.AlignTop
            Layout.fillWidth: true
            Layout.preferredWidth: 520
            Layout.minimumWidth: 0
            spacing: 4

            Text {
                objectName: "namesIntro"
                Layout.fillWidth: true
                Layout.maximumWidth: 620
                Layout.bottomMargin: 18
                text: "These words aren't in the dictionary, so the voice guesses how to say them. "
                      + "Press play to hear one. If it sounds wrong, type it the way it sounds."
                color: Theme.muted
                font.family: Theme.sans
                font.pixelSize: 15
                lineHeightMode: Text.FixedHeight
                lineHeight: 24
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }

            // NAME · IN THE BOOK · SAY IT AS, over a 1 px line.
            Item {
                objectName: "namesHead"
                Layout.fillWidth: true
                implicitHeight: headName.implicitHeight + 10 + 1
                Accessible.ignored: true

                MonoLabel {
                    id: headName
                    text: "NAME"
                    size: 11
                    tracking: 0.14
                }

                MonoLabel {
                    visible: !page.narrow
                    x: table.nameWidth + table.gap
                    text: "IN THE BOOK"
                    size: 11
                    tracking: 0.14
                }

                MonoLabel {
                    x: page.narrow ? table.nameWidth + table.gap : table.nameWidth + 150 + 2 * table.gap
                    text: "SAY IT AS"
                    size: 11
                    tracking: 0.14
                }

                Rectangle {
                    anchors.bottom: parent.bottom
                    width: parent.width
                    height: 1
                    color: Theme.line
                }
            }

            Repeater {
                model: page.shownCount
                delegate: NameRow {
                    Layout.fillWidth: true
                    nameWidth: table.nameWidth
                    sayWidth: table.sayWidth
                    gap: table.gap
                }
            }

            PillButton {
                objectName: "showAllNames"
                visible: Backend.names.length > page.firstRows
                Layout.topMargin: 14
                text: page.showAll ? "Show fewer" : "Show all " + Backend.names.length + " names"
                onClicked: page.showAll = !page.showAll
            }
        }

        // Try any word, and every fix.
        ColumnLayout {
            objectName: "namesSide"
            Layout.alignment: Qt.AlignTop | Qt.AlignLeft
            Layout.fillWidth: !body.sideBySide
            Layout.preferredWidth: 360
            Layout.maximumWidth: 440
            spacing: 22

            MonoLabel {
                text: "TRY ANY WORD"
                size: 11
                tracking: 0.14
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 16

                LineField {
                    id: tryField
                    objectName: "tryField"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Accessible.name: "A word or sentence to hear"
                    onAccepted: if (tryButton.enabled) Backend.playText(text)
                }

                RoundButton {
                    id: tryButton
                    objectName: "tryButton"
                    glyph: Backend.tryPlaying ? "pause" : "play"
                    enabled: Backend.previewAvailable && (Backend.tryPlaying || tryField.text.trim() !== "")
                    text: !Backend.previewAvailable
                          ? "Unavailable: no audio player was found (pw-play, paplay or aplay)"
                          : Backend.tryPlaying ? "Stop" : "Hear it"
                    onClicked: Backend.playText(tryField.text)
                }
            }

            DotMeter {
                objectName: "tryWave"
                visible: Backend.previewAvailable
                Layout.fillWidth: true
                count: page.levels.length
                dotSize: Theme.dotSize
                dotSpacing: Theme.dotGap
                singleLine: true
                colorAt: function(index) {
                    if (Backend.tryPlaying && index < 22)
                        return index === 21 ? Theme.accent : Theme.text
                    return page.levels[index] > 5 ? Theme.muted : Theme.wait
                }
                Accessible.ignored: true
            }

            MonoLabel {
                objectName: "tryUnavailable"
                visible: !Backend.previewAvailable
                Layout.fillWidth: true
                text: "NO AUDIO PLAYER FOUND · PW-PLAY, PAPLAY OR APLAY"
                size: 11
                wrapMode: Text.Wrap
            }

            MonoLabel {
                objectName: "heardAs"
                Layout.fillWidth: true
                text: "HEARD AS " + Backend.voiceLabel + " · " + Backend.speed.toFixed(2) + "×"
                size: 11
                tracking: 0.12
                wrapMode: Text.Wrap
            }

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: 1
                color: Theme.line
            }

            MonoLabel {
                objectName: "fixesCount"
                text: "YOUR FIXES · " + Backend.fixes.length
                size: 11
                tracking: 0.14
            }

            ColumnLayout {
                objectName: "fixList"
                Layout.fillWidth: true
                spacing: 0
                Accessible.role: Accessible.List
                Accessible.name: "Your fixes"

                Repeater {
                    model: Backend.fixes
                    delegate: FixRow {
                        Layout.fillWidth: true
                    }
                }
            }

            Text {
                objectName: "fixesNote"
                Layout.fillWidth: true
                text: "This book: saved next to the book, as " + Backend.bookListName
                      + ". Every book: used for all your books."
                color: Theme.muted
                font.family: Theme.mono
                font.pixelSize: 11
                font.letterSpacing: 11 * 0.08
                font.capitalization: Font.AllUppercase
                lineHeightMode: Text.FixedHeight
                lineHeight: 18.7
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }
        }
    }

    // Spare height stays at the bottom.
    Item { Layout.fillHeight: true; Layout.topMargin: -page.sectionGap }
}
