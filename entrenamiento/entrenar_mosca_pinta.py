from ultralytics import YOLO


def main():

    # ==========================================
    # CONFIGURACIÓN
    # ==========================================

    DATASET = r"C:\Users\crist\Downloads\MoscaPintaProyecto2\Datasetmp\data.yaml"

    MODELO = "yolov8s.pt"

    EPOCHS = 100
    IMG_SIZE = 640
    BATCH = 4

    # ==========================================
    # CARGAR MODELO
    # ==========================================

    model = YOLO(MODELO)

    # ==========================================
    # ENTRENAMIENTO
    # ==========================================

    results = model.train(
        data=DATASET,

        epochs=EPOCHS,
        imgsz=IMG_SIZE,
        batch=BATCH,

        project=r"C:\Users\crist\Downloads\MoscaPintaProyecto2\runs",
        name="moscapinta_holdout",

        patience=15,

        seed=42,

        device=0,

        # IMPORTANTE EN WINDOWS
        workers=0
    )

    # ==========================================
    # VALIDACIÓN
    # ==========================================

    print("\n======================================")
    print("EVALUACIÓN DEL CONJUNTO DE VALIDACIÓN")
    print("======================================")

    metrics_val = model.val(
        data=DATASET,
        split="val",
        imgsz=IMG_SIZE,
        batch=BATCH,
        device=0,
        workers=0
    )

    print("\nResultados de validación:")
    print(f"Precision:   {metrics_val.box.mp:.4f}")
    print(f"Recall:      {metrics_val.box.mr:.4f}")
    print(f"mAP50:       {metrics_val.box.map50:.4f}")
    print(f"mAP50-95:    {metrics_val.box.map:.4f}")

    # ==========================================
    # EVALUACIÓN FINAL - TEST
    # ==========================================

    print("\n======================================")
    print("EVALUACIÓN FINAL DEL CONJUNTO DE TEST")
    print("======================================")

    metrics_test = model.val(
        data=DATASET,
        split="test",
        imgsz=IMG_SIZE,
        batch=BATCH,
        device=0,
        workers=0
    )

    print("\nResultados finales:")
    print(f"Precision:   {metrics_test.box.mp:.4f}")
    print(f"Recall:      {metrics_test.box.mr:.4f}")
    print(f"mAP50:       {metrics_test.box.map50:.4f}")
    print(f"mAP50-95:    {metrics_test.box.map:.4f}")

    print("\n======================================")
    print("PROCESO TERMINADO")
    print("======================================")


# ==========================================
# EJECUCIÓN PRINCIPAL
# ==========================================

if __name__ == "__main__":
    main()