#!/usr/bin/env python3
"""
Download datasets and pre-trained models from Roboflow Universe.

Requires a free Roboflow API key: https://app.roboflow.com/settings/api

Usage:
    pip install roboflow ultralytics

    # Download the AIOT INNOWORK spaghetti dataset (CC BY 4.0)
    # 5.9k images, 3 classes: spaghetti, stringing, zits
    python download_roboflow.py --api-key YOUR_KEY --dataset aiot

    # Download the SpaghettiDetect multi-class dataset (CC BY 4.0)
    # 13 classes including spaghetti, bed adhesion failure, layer shift
    python download_roboflow.py --api-key YOUR_KEY --dataset multiclass

    # Train on downloaded dataset
    python train.py --data dataset/data.yaml --epochs 50

    # Or just download the dataset for labeling/review
    python download_roboflow.py --api-key YOUR_KEY --dataset aiot --dataset-only
"""

import argparse
import sys
from pathlib import Path

DATASETS = {
    "aiot": {
        "workspace": "aiot-innowork",
        "project": "spaghetti-mckhg",
        "version": 1,
        "description": "AIOT INNOWORK — 5.9k images, 3 classes (spaghetti, stringing, zits)",
        "license": "CC BY 4.0",
    },
    "multiclass": {
        "workspace": "spaghettidetect",
        "project": "3d-printing-flaws",
        "version": 1,
        "description": "SpaghettiDetect — 13 classes (spaghetti, blobs, cracks, etc.)",
        "license": "CC BY 4.0",
    },
    "spaghetti3d": {
        "workspace": "3d-printing-failure",
        "project": "spaghetti-3d",
        "version": 1,
        "description": "Spaghetti 3D — 715 images, focused spaghetti detection",
        "license": "CC BY 4.0",
    },
}


def main():
    parser = argparse.ArgumentParser(description="Download Roboflow datasets for O.D.I.N. Vigil AI")
    parser.add_argument("--api-key", required=True, help="Roboflow API key (free at app.roboflow.com/settings/api)")
    parser.add_argument("--dataset", required=True, choices=list(DATASETS.keys()),
                        help="Which dataset to download")
    parser.add_argument("--output", default="dataset", help="Output directory (default: dataset/)")
    parser.add_argument("--dataset-only", action="store_true",
                        help="Only download dataset, don't train or export")
    parser.add_argument("--export-onnx", action="store_true",
                        help="Train YOLOv8n and export to ONNX after download")
    parser.add_argument("--epochs", type=int, default=50, help="Training epochs if --export-onnx")
    args = parser.parse_args()

    try:
        from roboflow import Roboflow
    except ImportError:
        print("Error: roboflow not installed. Run: pip install roboflow")
        sys.exit(1)

    ds = DATASETS[args.dataset]
    print(f"Dataset: {ds['description']}")
    print(f"License: {ds['license']}")
    print()

    rf = Roboflow(api_key=args.api_key)
    project = rf.workspace(ds["workspace"]).project(ds["project"])
    version = project.version(ds["version"])

    print(f"Downloading to {args.output}/...")
    dataset = version.download("yolov8", location=args.output)
    print(f"Dataset downloaded to: {args.output}/")

    if args.dataset_only:
        print("Done (dataset only).")
        return

    if args.export_onnx:
        try:
            from ultralytics import YOLO
        except ImportError:
            print("Error: ultralytics not installed. Run: pip install ultralytics torch")
            sys.exit(1)

        data_yaml = Path(args.output) / "data.yaml"
        if not data_yaml.exists():
            print(f"Error: {data_yaml} not found")
            sys.exit(1)

        print(f"\nTraining YOLOv8n for {args.epochs} epochs...")
        model = YOLO("yolov8n.pt")
        model.train(
            data=str(data_yaml),
            epochs=args.epochs,
            imgsz=640,
            batch=16,
            project="runs/detect",
            name="roboflow_train",
            exist_ok=True,
        )

        best = Path("runs/detect/roboflow_train/weights/best.pt")
        if best.exists():
            print("\nExporting to ONNX...")
            trained = YOLO(str(best))
            trained.export(format="onnx", opset=17, imgsz=640)
            onnx_path = best.with_suffix(".onnx")
            print(f"\nONNX model ready: {onnx_path}")
            print("Upload to O.D.I.N. via Settings > Vigil AI > Upload Model")
        else:
            print("Training failed — best.pt not found")
    else:
        print(f"\nDataset ready. To train:")
        print(f"  python train.py --data {args.output}/data.yaml --epochs 50")


if __name__ == "__main__":
    main()
