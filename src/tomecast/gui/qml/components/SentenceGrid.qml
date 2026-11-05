// The sentence grid (DotGrid from Python), sized to the room it has: 5 px dots and at most
// 70 columns as in the wide mockup, or 3 px dots with 2 px gaps filling a corner window.
import QtQuick
import TomecastNative
import ".."

DotGrid {
    id: grid

    property bool compact: false
    // The width the grid may use; the column count follows it.
    property real availableWidth: 0

    // Corner window: as many 5 px columns as fit (72 at 400 px wide, as in its mockup).
    // Wide: the mockup's 70 columns, fewer if they don't fit.
    readonly property int pitch: dotSize + gap

    dotSize: compact ? 3 : 5
    gap: compact ? 2 : 3
    columns: Math.max(1, compact ? Math.floor(availableWidth / pitch)
                                 : Math.min(70, Math.floor((availableWidth + gap) / pitch)))
    doneColor: Theme.text
    currentColor: Theme.accent
    waitColor: Theme.wait

    Accessible.role: Accessible.ProgressBar
    Accessible.name: grid.description
}
