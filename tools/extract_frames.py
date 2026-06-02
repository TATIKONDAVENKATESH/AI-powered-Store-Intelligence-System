"""Extract one frame from each video."""

from pathlib import Path

import cv2


VIDEOS_DIR = Path(
    "data/Videos"
)

OUTPUT_DIR = Path(
    "data/camera_frames"
)

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


def extract_frame(
    video_path: Path,
) -> None:

    cap = cv2.VideoCapture(
        str(video_path)
    )

    if not cap.isOpened():

        print(
            f"Failed: {video_path.name}"
        )

        return

    total_frames = int(
        cap.get(
            cv2.CAP_PROP_FRAME_COUNT
        )
    )

    frame_index = (
        total_frames // 2
    )

    cap.set(
        cv2.CAP_PROP_POS_FRAMES,
        frame_index,
    )

    success, frame = cap.read()

    if not success:

        print(
            f"Could not read frame: {video_path.name}"
        )

        cap.release()

        return

    output_file = (
        OUTPUT_DIR
        / f"{video_path.stem}.jpg"
    )

    cv2.imwrite(
        str(output_file),
        frame,
    )

    print(
        f"Saved: {output_file}"
    )

    cap.release()


def main() -> None:

    for video_file in (
        VIDEOS_DIR.glob("*.mp4")
    ):

        extract_frame(
            video_file
        )


if __name__ == "__main__":

    main()