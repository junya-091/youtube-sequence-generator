// add_transitions.jsx  v1
// Premiere Pro ExtendScript
// analysis_debug.json の transition_events を読み取り、V1 クリップ境界にトランジションを自動適用
//
// 使い方:
//   1. YouTube Sequence をアクティブにする
//   2. analysis_debug.json が sequence.xml と同じ output/ フォルダにあること
//   3. ExtendScript Toolkit / Loader Script Panel で実行

var TRACK_INDEX = 0; // V1 (0-indexed)
var TOLERANCE_SEC = 0.5; // クリップ境界とのマッチング許容範囲 (秒)
var TICKS_PER_SECOND = 254016000000; // Premiere Pro 内部 tick レート

// トランジション名マッピング (英語/日本語)
var TRANSITION_MAP = {
    "cross_dissolve": ["Cross Dissolve", "クロスディゾルブ"],
    "dip_to_black": ["Dip to Black", "暗転"],
    "dip_to_white": ["Dip to White", "ホワイトアウト"]
};

// ── JSON パース (ExtendScript 互換) ──
function parseJSON(str) {
    try {
        return eval("(" + str + ")");
    } catch (e) {
        return null;
    }
}

// ── ファイル読み込み ──
function readFile(path) {
    var f = new File(path);
    if (!f.exists) return null;
    f.open("r");
    f.encoding = "UTF-8";
    var content = f.read();
    f.close();
    return content;
}

// ── QE API でトランジションオブジェクトを取得 ──
function findTransition(typeName) {
    var names = TRANSITION_MAP[typeName];
    if (!names) names = ["Cross Dissolve", "クロスディゾルブ"];
    for (var i = 0; i < names.length; i++) {
        try {
            var t = qe.project.getVideoTransitionByName(names[i]);
            if (t) return t;
        } catch (e) {}
    }
    return null;
}

// ── メイン ──
(function () {
    var seq = app.project.activeSequence;
    if (!seq) {
        alert("エラー: アクティブなシーケンスが見つかりません。");
        return;
    }

    // analysis_debug.json を探す
    var jsonPath = File.openDialog("analysis_debug.json を選択", "JSON:*.json", false);
    if (!jsonPath) return;

    var content = readFile(jsonPath.fsName);
    if (!content) {
        alert("エラー: ファイルを読み込めませんでした。");
        return;
    }

    var analysis = parseJSON(content);
    if (!analysis || !analysis.transition_events) {
        alert("エラー: transition_events が見つかりません。\n" +
              "create_youtube_sequence.py を再実行して最新の analysis_debug.json を生成してください。");
        return;
    }

    var events = analysis.transition_events;
    if (events.length === 0) {
        alert("transition_events は空です。トランジション適用なし。");
        return;
    }

    var track = seq.videoTracks[TRACK_INDEX];
    if (!track || track.clips.numItems < 2) {
        alert("V1 にクリップが2つ以上ありません。トランジション適用には最低2クリップ必要です。");
        return;
    }

    // V1 クリップ境界を収集 (各クリップの end.seconds)
    var boundaries = [];
    for (var i = 0; i < track.clips.numItems - 1; i++) {
        boundaries.push({
            index: i,
            endSec: track.clips[i].end.seconds,
            nextStartSec: track.clips[i + 1].start.seconds
        });
    }

    var report = "=== トランジション自動適用 ===\n";
    report += "イベント数: " + events.length + "\n";
    report += "クリップ境界数: " + boundaries.length + "\n\n";

    // QE API 初期化
    var qeSeq = null;
    var qeTrack = null;
    var qeAvailable = false;
    try {
        qeSeq = qe.project.getActiveSequence();
        if (qeSeq) {
            qeTrack = qeSeq.getVideoTrackAt(TRACK_INDEX);
            if (qeTrack) qeAvailable = true;
        }
    } catch (e) {
        report += "⚠ QE API 初期化失敗: " + e.message + "\n";
    }

    if (!qeAvailable) {
        alert("⚠ QE API が利用できません。\n" +
              "トランジション自動追加にはQE API対応のPremiere Proバージョンが必要です。\n\n" +
              "手動操作:\n" +
              "  1. エフェクトパネル → ビデオトランジション → ディゾルブ\n" +
              "  2. 各カットポイントにドラッグ\n\n" +
              "対象カットポイント:\n" +
              (function () {
                  var s = "";
                  for (var j = 0; j < events.length; j++) {
                      var e = events[j];
                      var sec = e.at_ms / 1000;
                      var m = Math.floor(sec / 60);
                      var ss = Math.floor(sec % 60);
                      s += "  " + m + ":" + (ss < 10 ? "0" : "") + ss + " - " + e.type + " (" + e.reason + ")\n";
                  }
                  return s;
              })());
        return;
    }

    var applied = 0;
    var skipped = 0;
    var failed = 0;

    for (var ei = 0; ei < events.length; ei++) {
        var ev = events[ei];
        var targetSec = ev.at_ms / 1000;
        var durationMs = ev.duration_ms || 1000;
        var typeName = ev.type || "cross_dissolve";

        // 最も近いクリップ境界を探す
        var bestIdx = -1;
        var bestDist = TOLERANCE_SEC + 1;
        for (var bi = 0; bi < boundaries.length; bi++) {
            var dist = Math.abs(boundaries[bi].endSec - targetSec);
            if (dist < bestDist) {
                bestDist = dist;
                bestIdx = bi;
            }
        }

        var timeFmt = Math.floor(targetSec / 60) + ":" +
                      (Math.floor(targetSec % 60) < 10 ? "0" : "") + Math.floor(targetSec % 60);

        if (bestIdx < 0 || bestDist > TOLERANCE_SEC) {
            report += "⏭ " + timeFmt + " (" + typeName + "): クリップ境界が見つからない (距離=" +
                      bestDist.toFixed(2) + "s)\n";
            skipped++;
            continue;
        }

        // トランジションオブジェクトを取得
        var transObj = findTransition(typeName);
        if (!transObj) {
            report += "⚠ " + timeFmt + ": トランジション \"" + typeName + "\" が見つかりません\n";
            failed++;
            continue;
        }

        // QE API でトランジション適用
        // 前のクリップの末尾に適用
        var durationTicks = Math.round(durationMs / 1000 * TICKS_PER_SECOND).toString();
        try {
            var qeItem = qeTrack.getItemAt(bestIdx);
            qeItem.addTransition(transObj, true, durationTicks);
            report += "✅ " + timeFmt + " (" + typeName + ", " + durationMs + "ms): クリップ " +
                      bestIdx + "-" + (bestIdx + 1) + " 間に適用\n";
            applied++;
        } catch (e) {
            report += "❌ " + timeFmt + " (" + typeName + "): 適用失敗 - " + e.message + "\n";
            failed++;
        }
    }

    report += "\n=== 結果 ===\n";
    report += "適用: " + applied + " / スキップ: " + skipped + " / 失敗: " + failed + "\n";
    report += "✅ 完了";

    alert(report);
})();
