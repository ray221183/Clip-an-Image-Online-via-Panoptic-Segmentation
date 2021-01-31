//////////////////////////////
//last updated date: Sep. 23, 2020
//////////////////////////////
//////////////////////////////

let middleImg = document.getElementById("display-1");
let idImg = document.getElementById("display-2");
let resultImg = document.getElementById("display-3");
let idContext = idImg.getContext('2d');
let resultContext = resultImg.getContext('2d');



let srcImg2 = new Image();
let srcImg3 = new Image();
let data = {{data | safe}};

let middleImgSize = [0.0, 0.0]
let middleImgPos = [0.0, 0.0, 0.0, 0.0]
let selectedId = new Map()

middleImg.src = "/static/postImages/" + data + ".jpg"
srcImg2.src = "/static/idList/" + data + ".png"
srcImg3.src = "/static/selectedRegion/" + data + ".png"


middleImg.onload = function() {
    console.log("loaded1")
    let Info = getImgSizeInfo(middleImg)
    console.log(Info)
    middleImgSize = [Info.width, Info.height]
    middleImgPos = [Info.top, Info.right, Info.bottom, Info.left]
}
srcImg2.onload = function() {
    console.log("loaded2")
    var w = srcImg2.width, h = srcImg2.height;
    idImg.width = w;
    idImg.height = h;
    idContext.drawImage(srcImg2, 0, 0, w, h);
    idContext.fillText('Mood', 163, 191);
}
srcImg3.onload = function() {
    console.log("loaded3")
    var w = srcImg3.width, h = srcImg3.height;
    resultImg.width = w;
    resultImg.height = h;
    resultContext.drawImage(srcImg3, 0, 0, w, h);
    resultContext.fillText('Mood', 163, 191);
}


middleImg.addEventListener('click', (e) => {
    console.log('click middleImg')
    console.log(e.offsetX, e.offsetY, middleImgPos)
    if( e.offsetX <= middleImgPos[1] &&  e.offsetX >= middleImgPos[3] && e.offsetY <= middleImgPos[2] && e.offsetY >= middleImgPos[0]){
        let x = e.offsetX - middleImgPos[3]
        let y = e.offsetY - middleImgPos[0]
        x = Math.round(x * (middleImg.naturalWidth/middleImgSize[0]))
        y = Math.round(y * (middleImg.naturalHeight/middleImgSize[1]))
        console.log(x, y)
        var data = idContext.getImageData(x, y, 1, 1).data;
        console.log(data[0])
        console.log("1: ", resultContext.getImageData(0, 0, middleImg.naturalWidth, middleImg.naturalHeight).data)
        modifyResultImg(data[0])
    }
})

function modifyResultImg( curId ){
    console.log("start")
    genIdList(curId)
    console.log("middle")
    let idData = idContext.getImageData(0, 0, middleImg.naturalWidth, middleImg.naturalHeight).data
    console.log("2: ", resultContext.getImageData(0, 0, middleImg.naturalWidth, middleImg.naturalHeight).data)
    let result = resultContext.getImageData(0, 0, middleImg.naturalWidth, middleImg.naturalHeight);
    for(let j=0; j<srcImg3.height; j++){
      for (let i=0; i<srcImg3.width; i++){
            let curidx = (i + j * srcImg3.width) * 4
            if (!selectedId.get(idData[curidx])) {
                result.data[(i *4+ j*srcImg3.width*4)+3] = 4
            }
            else {
                result.data[(i *4+ j*srcImg3.width*4)+3] = 255
            }
        }
    }
    console.log("end")
    console.log("3", result.data)
    resultContext.putImageData(result, 0, 0);
    console.log("4: ", resultContext.getImageData(0, 0, middleImg.naturalWidth, middleImg.naturalHeight).data)
}

function genIdList( curId ){
    if(!selectedId.has(curId)){
        selectedId.set(curId, true)
    }
    else{
        selectedId.set(curId, !selectedId.get(curId))
    }
    console.log(selectedId)
}


function getRenderedSize(contains, cWidth, cHeight, width, height, pos){
    console.log(contains, cWidth, cHeight, width, height, pos)
    var oRatio = width / height,
        cRatio = cWidth / cHeight;
    return function() {
      if (contains ? (oRatio > cRatio) : (oRatio < cRatio)) {
        this.width = cWidth;
        this.height = cWidth / oRatio;
      } else {
        this.width = cHeight * oRatio;
        this.height = cHeight;
      }      
      this.left = (cWidth - this.width)*(pos/100);
      this.right = this.width + this.left;
      this.top = (cHeight - this.height)*(pos/100);
      this.bottom = this.height + this.top;
      return this;
    }.call({});
}
  
function getImgSizeInfo(img) {
    var pos = window.getComputedStyle(img).getPropertyValue('object-position').split(' ');
    console.log(pos)
    return getRenderedSize(true,
                            img.width,
                            img.height,
                            img.naturalWidth,
                            img.naturalHeight,
                            parseInt(pos[0]));
}

  