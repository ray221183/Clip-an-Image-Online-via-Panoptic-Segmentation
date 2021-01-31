//////////////////////////////
//last updated date: Sep. 23, 2020
//////////////////////////////
//////////////////////////////

let middleImg = document.getElementById("display-1");
let canvas = document.getElementById("display-2");
let context = canvas.getContext('2d');

let srcImg = new Image();
srcImg.src = "/static/images/uploadedImg.jpg"

console.log("clip.js")

let pan_segm_id = {{pan_segm_id | tojson}}
console.log(pan_segm_id)
    
srcImg.onload = function() {
    console.log("loaded")
    var w = srcImg.width, h = srcImg.height;
    canvas.width = w;
    canvas.height = h;
    context.drawImage(srcImg, 0, 0, w, h);
    context.fillText('Mood', 163, 191);
}


middleImg.addEventListener('click', () => {
    console.log('click')
})

