"""
月間スケジュール自動化ツール

3つのブロックに分かれた自動化コマンドを提供します:

Block 1 (expand): 週間訪問パターンを月間スケジュールに展開
    python main.py expand --month 2026-04

Block 2 (export): 月間スケジュールをCSVで出力
    python main.py export --month 2026-04 --out data/current_202604.csv

Block 3 (apply): 最適化されたスケジュールとの差分を適用
    python main.py apply --month 2026-04 --week-start 2026-04-20 --current data/current.csv --optimized data/opt_week.csv

共通オプション:
    --headed    ブラウザを表示して実行
    --dry-run   テスト実行（実際の操作は行わない）
"""

import sys
import argparse


def main():
    parser = argparse.ArgumentParser(
        description="月間スケジュール自動化ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python main.py expand --month 2026-04
  python main.py export --month 2026-04 --out data/current_202604.csv
  python main.py apply --month 2026-04 --week-start 2026-04-20 --current data/current.csv --optimized data/opt.csv

注意: デフォルトは4月(2026-04)です。他の月を指定すると警告が表示されます。
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="実行するコマンド")

    # Block 1: expand
    expand_parser = subparsers.add_parser(
        "expand",
        help="Block 1: 週間訪問パターンを月間スケジュールに展開"
    )
    expand_parser.add_argument("--month", default="2026-04", help="対象月 (デフォルト: 2026-04)")
    expand_parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    expand_parser.add_argument("--dry-run", action="store_true", help="テスト実行")

    # Block 2: export
    export_parser = subparsers.add_parser(
        "export",
        help="Block 2: 月間スケジュールをCSVで出力"
    )
    export_parser.add_argument("--month", default="2026-04", help="対象月 (デフォルト: 2026-04)")
    export_parser.add_argument("--out", default=None, help="出力ファイルパス")
    export_parser.add_argument("--headed", action="store_true", help="ブラウザを表示")

    # Block 3: apply
    apply_parser = subparsers.add_parser(
        "apply",
        help="Block 3: 差分を検出して適用"
    )
    apply_parser.add_argument("--month", default="2026-04", help="対象月 (デフォルト: 2026-04)")
    apply_parser.add_argument("--week-start", required=True, help="対象週の開始日 (例: 2026-04-20)")
    apply_parser.add_argument("--current", required=True, help="現在のスケジュールCSVパス")
    apply_parser.add_argument("--optimized", required=True, help="最適化後のスケジュールCSVパス")
    apply_parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    apply_parser.add_argument("--dry-run", action="store_true", help="テスト実行（差分計算のみ）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # 4月以外は警告
    if hasattr(args, "month") and not args.month.endswith("-04"):
        print("警告: 4月以外の月が指定されています！")
        print("  1月・2月・3月はいじらないでください。")
        confirm = input("続行しますか？ (y/N): ")
        if confirm.lower() != "y":
            print("キャンセルしました")
            sys.exit(0)

    if args.command == "expand":
        from commands.expand import run_expand
        run_expand(
            month=args.month,
            headless=not args.headed,
            dry_run=args.dry_run,
        )

    elif args.command == "export":
        from commands.export import run_export
        run_export(
            month=args.month,
            out_path=args.out,
            headless=not args.headed,
        )

    elif args.command == "apply":
        from commands.apply import run_apply
        run_apply(
            month=args.month,
            week_start=args.week_start,
            current_csv=args.current,
            optimized_csv=args.optimized,
            headless=not args.headed,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
