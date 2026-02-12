#!/usr/bin/env python3
"""
O.D.I.N. Vigil AI — Training Script

Fine-tunes YOLOv8-nano on exported training data from the O.D.I.N. vision system.
Runs OUTSIDE the Docker container on a machine with a GPU (or CPU, slower).

Prerequisites:
    pip install ultralytics torch

Usage:
    # Download training data from O.D.I.N.
    curl -H "Authorization: Bearer <token>" \
         http://localhost:8000/api/vision/training-data/export \
         -o training_data.zip

    # Unzip
    unzip training_data.zip -d dataset/

    # Train (GPU recommended)
    python train.py --data dataset/data.yaml --epochs 50

    # Upload resulting model
    curl -X POST "http://localhost:8000/api/vision/models?name=spaghetti_v1&detection_type=spaghetti&input_size=640" \
         -H "Authorization: Bearer <token>" \
         -F "file=@runs/detect/train/weights/best.onnx"
"""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Train YOLOv8-nano for O.D.I.N. Vigil AI")
    parser.add_argument("--data", required=True, help="Path to data.yaml from exported dataset")
    parser.add_argument("--base-model", default="yolov8n.pt", help="Base model (default: yolov8n.pt)")
    parser.add_argument("--epochs", type=int, default=50, help="Training epochs")
    parser.add_argument("--imgsz", type=int, default=640, help="Input image size")
    parser.add_argument("--batch", type=int, default=16, help="Batch size")
    parser.add_argument("--device", default=None, help="Device: 0 for GPU, cpu for CPU")
    parser.add_argument("--export-only", action="store_true", help="Skip training, just export last best.pt to ONNX")
    parser.add_argument("--weights", default=None, help="Path to .pt weights for export-only mode")
    args = parser.parse_args()

    try:
        from ultralytics import YOLO
    except ImportError:
        print("Error: ultralytics not installed.")
        print("Install with: pip install ultralytics torch")
        sys.exit(1)

    if args.export_only:
        weights = args.weights or "runs/detect/train/weights/best.pt"
        print(f"Exporting {weights} to ONNX...")
        model = YOLO(weights)
        model.export(format="onnx", opset=17, imgsz=args.imgsz)
        print("Done. ONNX model saved next to the .pt file.")
        return

    data_path = Path(args.data).resolve()
    if not data_path.exists():
        print(f"Error: {data_path} not found")
        sys.exit(1)

    print(f"Training YOLOv8-nano on {data_path}")
    print(f"  Base model: {args.base_model}")
    print(f"  Epochs: {args.epochs}")
    print(f"  Image size: {args.imgsz}")
    print(f"  Batch size: {args.batch}")

    model = YOLO(args.base_model)

    results = model.train(
        data=str(data_path),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project="runs/detect",
        name="train",
        exist_ok=True,
        patience=10,
        save=True,
        plots=True,
    )

    # Export to ONNX
    best_weights = Path("runs/detect/train/weights/best.pt")
    if best_weights.exists():
        print("\nExporting best model to ONNX...")
        best_model = YOLO(str(best_weights))
        best_model.export(format="onnx", opset=17, imgsz=args.imgsz)
        onnx_path = best_weights.with_suffix(".onnx")
        print(f"\nONNX model saved to: {onnx_path}")
        print(f"Upload to O.D.I.N. via Settings > Vigil AI > Upload Model")
    else:
        print("Warning: best.pt not found, skipping ONNX export")

    print("\nTraining complete.")


if __name__ == "__main__":
    main()
