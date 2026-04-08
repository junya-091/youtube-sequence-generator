// clip_alignment.jsx  v1
// Premiere Pro ExtendScript
// テロップクリップ間の微小ギャップ（数フレーム）を自動修正する
//
// 使い方:
//   1. YouTube Sequence をアクティブにする
//   2. ExtendScript Toolkit / Loader Script Panel で実行

var TRACK_INDEX = 0; // V1 (0-indexed)
var MAX_GAP_FRAMES = 5; // この値以下のフレーム数ギャップを修正

(function () {
    var seq = app.project.activeSequence;
    if (!seq) {
        alert("\u30A8\u30E9\u30FC: \u30A2\u30AF\u30C6\u30A3\u30D6\u306A\u30B7\u30FC\u30B1\u30F3\u30B9\u304C\u898B\u3064\u304B\u308A\u307E\u305B\u3093\u3002");
        return;
    }

    var track = seq.videoTracks[TRACK_INDEX];
    if (!track || track.clips.numItems < 2) {
        alert("V1 \u306B\u30AF\u30EA\u30C3\u30D7\u304C2\u3064\u4EE5\u4E0A\u5FC5\u8981\u3067\u3059\u3002");
        return;
    }

    // fps を取得
    var fps = 29.97;
    try {
        var seqSettings = seq.getSettings();
        if (seqSettings && seqSettings.videoFrameRate) {
            fps = seqSettings.videoFrameRate.seconds ? (1.0 / seqSettings.videoFrameRate.seconds) : 29.97;
        }
    } catch (e) {}

    var maxGapSec = MAX_GAP_FRAMES / fps;
    var fixedCount = 0;
    var skippedCount = 0;

    // 前から順に隣接クリップ間のギャップをチェック
    for (var i = 0; i < track.clips.numItems - 1; i++) {
        var current = track.clips[i];
        var next = track.clips[i + 1];

        var gapSec = next.start.seconds - current.end.seconds;

        // ギャップなし or 重複（負の値）はスキップ
        if (gapSec <= 0.001) continue;

        var gapFrames = Math.round(gapSec * fps);

        if (gapFrames <= MAX_GAP_FRAMES) {
            // 前のクリップの end を次のクリップの start に延長
            try {
                var newEnd = new Time();
                newEnd.seconds = next.start.seconds;
                current.end = newEnd;
                fixedCount++;
            } catch (e) {
                skippedCount++;
            }
        } else {
            skippedCount++;
        }
    }

    var msg = "=== \u30AF\u30EA\u30C3\u30D7\u30AE\u30E3\u30C3\u30D7\u4FEE\u6B63\u7D50\u679C ===\n\n";
    msg += "\u4FEE\u6B63: " + fixedCount + " \u7B87\u6240\n";
    msg += "\u30B9\u30AD\u30C3\u30D7: " + skippedCount + " \u7B87\u6240\n";
    msg += "(\u95BE\u5024: " + MAX_GAP_FRAMES + " \u30D5\u30EC\u30FC\u30E0\u4EE5\u4E0B\u3092\u4FEE\u6B63)\n";
    msg += "\n\u2705 \u5B8C\u4E86";
    alert(msg);
})();
