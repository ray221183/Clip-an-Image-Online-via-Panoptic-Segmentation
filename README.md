# Clip-an-Image-Online-via-Panoptic-Segmentation

## 簡介
在網路上可以找到一些去被軟體，然而這些軟體多數不夠客製化，使用者無法選擇自己想要留哪些區域。因此我想要利用Panoptic Segmentation，讓model先分割出foreground以及background之後，使用者可以根據分割的結果，在網頁上點選想留下的區域，就留下該區域。如此一來，就可以讓去背這項功能更加客製化。

Demo影片連結： https://youtu.be/_al6dHuo6Zw
## 環境建置
```
Model: EPSNet  
Backend: Flask  
Frontend: HTML, CSS, JS, Jinja2 
```
## Citation
```
@article{EPSNet_arxiv,  
  title={EPSNet: Efficient Panoptic Segmentation Network with Cross-layer Attention Fusion},  
  author={Chia-Yuan Chang and Shuo-En Chang and P. Hsiao and L. Fu},  
  journal={ArXiv},  
  year={2020},  
  volume={abs/2003.10142}  
}
```