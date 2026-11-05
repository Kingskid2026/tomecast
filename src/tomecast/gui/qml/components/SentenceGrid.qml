// The sentence grid (DotGrid from Python): 5 px dots 3 px apart, at most 70 columns as in the
// mockups, fewer when the room is narrower (columns = min(70, floor(room / 8))), so it never
// overflows.
import QtQuick
import TomecastNative
import ".."

DotGrid {
    id: grid

    // The width the grid may use; the column count follows it.
    property real availableWidth: 0

    readonly property int pitch: dotSize + gap

    dotSize: Theme.dotSize
    gap: Theme.dotGap
    columns: Math.max(1, Math.min(70, Math.floor(availableWidth / pitch)))
    doneColor: Theme.text
    currentColor: Theme.accent
    waitColor: Theme.wait

    Accessible.role: Accessible.ProgressBar
    Accessible.name: grid.description
}
