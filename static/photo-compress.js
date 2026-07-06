// 上傳前在瀏覽器端壓縮照片（縮到 1600px JPEG），大幅加快手機遠端上傳
async function compressPhoto(file) {
    try {
        if (!file.type.startsWith('image/')) return file;
        let bmp;
        try {
            // from-image：依照片的 EXIF 方向自動轉正
            bmp = await createImageBitmap(file, {imageOrientation: 'from-image'});
        } catch (e) {
            bmp = await createImageBitmap(file);
        }
        const MAX = 1600;
        const scale = Math.min(1, MAX / Math.max(bmp.width, bmp.height));
        const w = Math.round(bmp.width * scale);
        const h = Math.round(bmp.height * scale);
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        canvas.getContext('2d').drawImage(bmp, 0, 0, w, h);
        bmp.close && bmp.close();
        const blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', 0.82));
        if (!blob || blob.size >= file.size) return file;  // 壓不了就傳原檔
        const name = (file.name || 'photo').replace(/\.[^.]+$/, '') + '.jpg';
        return new File([blob], name, {type: 'image/jpeg'});
    } catch (e) {
        return file;  // 任何失敗都退回原檔，確保照樣能傳
    }
}

async function compressPhotos(fileList) {
    const out = [];
    for (const f of fileList) out.push(await compressPhoto(f));
    return out;
}
