// convert_v2_to_adjustment.jsx  v2
// Premiere Pro ExtendScript
// V2 トラックのブラックビデオを削除し、調整レイヤー ProjectItem で overwriteClip する
//
// 使い方:
//   1. YouTube Sequence をアクティブにする
//   2. Loader Script Panel → Desktop/Adobe → このファイルを実行

var TRACK_INDEX = 1; // V2 (0-indexed: V1=0, V2=1)

// ── プロジェクトツリーから調整レイヤーを再帰検索 ──────────────────────
function findAdjItem(folder) {
    for (var i = 0; i < folder.children.numItems; i++) {
        var item = folder.children[i];
        try {
            if (item.type !== ProjectItemType.BIN && item.name === "調整レイヤー") {
                return item;
            }
        } catch (e) {}
        if (item.type === ProjectItemType.BIN) {
            var found = findAdjItem(item);
            if (found) return found;
        }
    }
    return null;
}

// ── メイン ────────────────────────────────────────────────────────────
var seq = app.project.activeSequence;
if (!seq) {
    alert("エラー: アクティブなシーケンスが見つかりません。\nYouTube Sequence を開いてください。");
} else {
    var adjItem = findAdjItem(app.project.rootItem);
    if (!adjItem) {
        alert("エラー: プロジェクトに「調整レイヤー」アイテムが見つかりません。\n" +
              "プロジェクトパネルで 新規アイテム → 調整レイヤー を作成してください。");
    } else {
        var track = seq.videoTracks[TRACK_INDEX];
        if (!track || track.clips.numItems === 0) {
            alert("V2 にクリップがありません。sequence.xml をインポート済みか確認してください。");
        } else {

            // ── 1. クリップ情報を収集 ──
            var infos = [];
            for (var i = 0; i < track.clips.numItems; i++) {
                var c = track.clips[i];
                infos.push({
                    startSec: c.start.seconds,
                    endSec:   c.end.seconds,
                    durSec:   c.duration.seconds
                });
            }

            // ── 2. 後ろから削除 ──
            for (var i = track.clips.numItems - 1; i >= 0; i--) {
                track.clips[i].remove(false, false);
            }

            // ── 3. 調整レイヤーを overwriteClip で配置 ──
            var placeErr = "";
            for (var i = 0; i < infos.length; i++) {
                try {
                    var t = new Time();
                    t.seconds = infos[i].startSec;
                    seq.overwriteClip(adjItem, t, TRACK_INDEX, -1);
                } catch (e) {
                    placeErr += "[" + i + "] 配置失敗: " + e.message + "\n";
                }
            }

            // ── 4. 長さを調整（clip.end へ代入を試みる）──
            var trimLog = "";
            for (var i = 0; i < track.clips.numItems; i++) {
                var clip = track.clips[i];
                // start が一致する info を探す
                for (var j = 0; j < infos.length; j++) {
                    if (Math.abs(clip.start.seconds - infos[j].startSec) < 0.05) {
                        var wantEnd = infos[j].endSec;
                        try {
                            var et = new Time();
                            et.seconds = wantEnd;
                            clip.end = et;
                            trimLog += "[" + j + "] end=" + wantEnd.toFixed(2) + "s → ";
                            trimLog += (Math.abs(clip.end.seconds - wantEnd) < 0.05) ? "OK\n" : "反映なし(" + clip.end.seconds.toFixed(2) + ")\n";
                        } catch (e) {
                            trimLog += "[" + j + "] end set error: " + e.message + "\n";
                        }
                        break;
                    }
                }
            }

            // ── 5. 結果レポート ──
            var msg = "=== V2 置き換え結果 ===\n\n";
            msg += "対象: " + infos.length + " クリップ\n";
            msg += "配置後クリップ数: " + track.clips.numItems + "\n";
            if (placeErr) msg += "\n[配置エラー]\n" + placeErr;
            if (trimLog)  msg += "\n[長さ調整]\n" + trimLog;
            msg += "\n✅ タイムラインのV2 クリップが「調整レイヤー」になっているか確認してください。";
            alert(msg);
        }
    }
}
