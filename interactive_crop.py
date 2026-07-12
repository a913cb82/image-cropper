import os
import tkinter as tk
from tkinter import filedialog
from PIL import Image, ImageTk, ImageOps, ImageDraw

class QuickCropper:
    def __init__(self, root):
        self.root = root
        self.root.title("Interactive 20:9 Cropper")
        self.root.geometry("1200x800")
        
        self.ratio = 20 / 9
        self.input_dir = None
        self.output_dir = None
        self.images = []
        self.current_idx = 0
        
        # Crop box state (normalized coordinates relative to the image: 0.0 to 1.0)
        self.cx = 0.5
        self.cy = 0.5
        self.cw = 0.8  # Initial crop box width (80% of image width)
        
        self.drag_start_x = None
        self.drag_start_y = None
        
        self.select_directories()
        if not self.images:
            self.root.destroy()
            return
            
        self.canvas = tk.Canvas(self.root, bg="#121212", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        self.canvas.bind("<Configure>", lambda e: self.update_view())
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<MouseWheel>", self.on_zoom)
        
        self.root.bind("<space>", lambda e: self.approve_and_next())
        self.root.bind("<Return>", lambda e: self.approve_and_next())
        self.root.bind("<Right>", lambda e: self.navigate(1))
        self.root.bind("<Left>", lambda e: self.navigate(-1))
        
        self.load_image()

    def select_directories(self):
        self.input_dir = filedialog.askdirectory(title="Select Input Folder")
        if not self.input_dir:
            return
        self.output_dir = os.path.join(self.input_dir, "cropped_20_9")
        os.makedirs(self.output_dir, exist_ok=True)
        
        valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".tiff"}
        self.images = sorted([
            os.path.join(self.input_dir, f) for f in os.listdir(self.input_dir)
            if os.path.splitext(f)[1].lower() in valid_exts
        ])

    def load_image(self):
        if not (0 <= self.current_idx < len(self.images)):
            print("Processing complete!")
            self.root.destroy()
            return
            
        self.img_path = self.images[self.current_idx]
        self.pil_img = Image.open(self.img_path)
        self.pil_img = ImageOps.exif_transpose(self.pil_img)  # Honor EXIF orientation
        
        self.cx, self.cy = 0.5, 0.5
        img_w, img_h = self.pil_img.size
        
        # Calculate maximum possible starting width for a 20:9 crop
        max_cw = min(1.0, (img_h * self.ratio) / img_w)
        self.cw = max_cw * 0.9
        
        self.constrain_crop()
        self.update_view()

    def constrain_crop(self):
        img_w, img_h = self.pil_img.size
        max_cw = min(1.0, (img_h * self.ratio) / img_w)
        self.cw = max(0.1, min(max_cw, self.cw))
        
        hw = self.cw / 2
        hh = (self.cw * img_w / self.ratio) / img_h / 2
        
        # Keep crop box strictly inside the image boundaries
        self.cx = max(hw, min(1.0 - hw, self.cx))
        self.cy = max(hh, min(1.0 - hh, self.cy))

    def update_view(self):
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w < 50 or canvas_h < 50:
            return
            
        img_w, img_h = self.pil_img.size
        scale = min(canvas_w / img_w, canvas_h / img_h)
        self.disp_w = int(img_w * scale)
        self.disp_h = int(img_h * scale)
        
        # Fast bilinear resize for interactive responsiveness
        resized = self.pil_img.resize((self.disp_w, self.disp_h), Image.Resampling.BILINEAR)
        composited = resized.convert("RGBA")
        
        # Dim overlay for non-selected area
        overlay = Image.new("RGBA", (self.disp_w, self.disp_h), (0, 0, 0, 160))
        draw = ImageDraw.Draw(overlay)
        
        disp_cw = self.cw * self.disp_w
        disp_ch = disp_cw / self.ratio
        disp_cx = self.cx * self.disp_w
        disp_cy = self.cy * self.disp_h
        
        x1 = int(disp_cx - disp_cw / 2)
        y1 = int(disp_cy - disp_ch / 2)
        x2 = int(x1 + disp_cw)
        y2 = int(y1 + disp_ch)
        
        # Punch out the active crop box to be transparent
        draw.rectangle([x1, y1, x2, y2], fill=(0, 0, 0, 0))
        composited.alpha_composite(overlay)
        
        # Draw a yellow target bounding box
        draw_border = ImageDraw.Draw(composited)
        draw_border.rectangle([x1, y1, x2, y2], outline=(255, 204, 0, 255), width=2)
        
        self.tk_img = ImageTk.PhotoImage(composited)
        self.offset_x = (canvas_w - self.disp_w) // 2
        self.offset_y = (canvas_h - self.disp_h) // 2
        
        self.canvas.delete("all")
        self.canvas.create_image(self.offset_x, self.offset_y, anchor=tk.NW, image=self.tk_img)
        
        text_info = (
            f"Image {self.current_idx + 1}/{len(self.images)}\n"
            f"File: {os.path.basename(self.img_path)}\n\n"
            "Controls:\n"
            "  [Left-Click + Drag] : Slide Crop Box\n"
            "  [Scroll Wheel]      : Zoom Crop Box\n"
            "  [Space] / [Enter]   : Approve & Save\n"
            "  [Left] / [Right]    : Navigate without saving"
        )
        self.canvas.create_text(20, 20, anchor=tk.NW, text=text_info, fill="#ffffff", font=("Consolas", 11, "bold"))

    def on_click(self, event):
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def on_drag(self, event):
        if self.drag_start_x is None or self.drag_start_y is None:
            return
        dx = event.x - self.drag_start_x
        dy = event.y - self.drag_start_y
        
        self.cx += dx / self.disp_w
        self.cy += dy / self.disp_h
        
        self.drag_start_x = event.x
        self.drag_start_y = event.y
        
        self.constrain_crop()
        self.update_view()

    def on_zoom(self, event):
        # Multiplier depends on scroll wheel direction
        factor = 0.95 if event.delta > 0 else 1.05
        self.cw *= factor
        self.constrain_crop()
        self.update_view()

    def approve_and_next(self):
        img_w, img_h = self.pil_img.size
        crop_w = self.cw * img_w
        crop_h = crop_w / self.ratio
        
        cx_px = self.cx * img_w
        cy_px = self.cy * img_h
        
        x1 = int(cx_px - crop_w / 2)
        y1 = int(cy_px - crop_h / 2)
        x2 = int(x1 + crop_w)
        y2 = int(y1 + crop_h)
        
        # High-quality save using Lanczos resampling for downstream exports
        cropped = self.pil_img.crop((x1, y1, x2, y2))
        dest_path = os.path.join(self.output_dir, os.path.basename(self.img_path))
        cropped.save(dest_path, quality=95, subsampling=0)
        
        print(f"Cropped and saved: {os.path.basename(self.img_path)}")
        self.navigate(1)

    def navigate(self, direction):
        self.current_idx += direction
        self.load_image()

if __name__ == "__main__":
    root = tk.Tk()
    app = QuickCropper(root)
    root.mainloop()